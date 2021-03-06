#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2003-2014 Edgewall Software
# Copyright (C) 2003-2005 Jonas Borgström <jonas@edgewall.com>
# Copyright (C) 2005 Christopher Lenz <cmlenz@gmx.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://trac.edgewall.org/log/.
#
# Author: Jonas Borgström <jonas@edgewall.com>
#         Christopher Lenz <cmlenz@gmx.de>

import abc
import doctest
import inspect
import os
import sys
import tempfile
import time
import types
import unittest

try:
    from babel import Locale
except ImportError:
    locale_en = None
else:
    locale_en = Locale.parse('en_US')

import trac.db.mysql_backend
import trac.db.postgres_backend
import trac.db.sqlite_backend
from trac.config import Configuration
from trac.core import ComponentManager
from trac.db.api import DatabaseManager, parse_connection_uri
from trac.env import Environment
from trac.ticket.default_workflow import load_workflow_config_snippet
from trac.util import translation


def Mock(bases=(), *initargs, **kw):
    """
    Simple factory for dummy classes that can be used as replacement for the
    real implementation in tests.

    Base classes for the mock can be specified using the first parameter, which
    must be either a tuple of class objects or a single class object. If the
    bases parameter is omitted, the base class of the mock will be object.

    So to create a mock that is derived from the builtin dict type, you can do:

    >>> mock = Mock(dict)
    >>> mock['foo'] = 'bar'
    >>> mock['foo']
    'bar'

    Attributes of the class are provided by any additional keyword parameters.

    >>> mock = Mock(foo='bar')
    >>> mock.foo
    'bar'

    Objects produces by this function have the special feature of not requiring
    the 'self' parameter on methods, because you should keep data at the scope
    of the test function. So you can just do:

    >>> mock = Mock(add=lambda x,y: x+y)
    >>> mock.add(1, 1)
    2

    To access attributes from the mock object from inside a lambda function,
    just access the mock itself:

    >>> mock = Mock(dict, do=lambda x: 'going to the %s' % mock[x])
    >>> mock['foo'] = 'bar'
    >>> mock.do('foo')
    'going to the bar'

    Because assignments or other types of statements don't work in lambda
    functions, assigning to a local variable from a mock function requires some
    extra work:

    >>> myvar = [None]
    >>> mock = Mock(set=lambda x: myvar.__setitem__(0, x))
    >>> mock.set(1)
    >>> myvar[0]
    1
    """
    if not isinstance(bases, tuple):
        bases = (bases,)

    # if base classes have abstractmethod and abstractproperty,
    # create dummy methods for abstracts
    attrs = {}
    def dummyfn(self, *args, **kwargs):
        raise NotImplementedError
    for base in bases:
        if getattr(base, '__metaclass__', None) is not abc.ABCMeta:
            continue
        fn = types.UnboundMethodType(dummyfn, None, base)
        for name, attr in inspect.getmembers(base):
            if name in attrs:
                continue
            if isinstance(attr, abc.abstractproperty) or \
                    isinstance(attr, types.UnboundMethodType) and \
                    getattr(attr, '__isabstractmethod__', False) is True:
                attrs[name] = fn

    cls = type('Mock', bases, attrs)
    mock = cls(*initargs)
    for k, v in kw.items():
        setattr(mock, k, v)
    return mock


class MockPerm(object):
    """Fake permission class. Necessary as Mock can not be used with operator
    overloading."""

    username = ''

    def has_permission(self, action, realm_or_resource=None, id=False,
                       version=False):
        return True
    __contains__ = has_permission

    def __call__(self, realm_or_resource, id=False, version=False):
        return self

    def require(self, action, realm_or_resource=None, id=False, version=False):
        pass
    assert_permission = require


class TestSetup(unittest.TestSuite):
    """
    Test suite decorator that allows a fixture to be setup for a complete
    suite of test cases.
    """
    def setUp(self):
        """Sets up the fixture, and sets self.fixture if needed"""
        pass

    def tearDown(self):
        """Tears down the fixture"""
        pass

    def run(self, result):
        """Setup the fixture (self.setUp), call .setFixture on all the tests,
        and tear down the fixture (self.tearDown)."""
        self.setUp()
        if hasattr(self, 'fixture'):
            for test in self._tests:
                if hasattr(test, 'setFixture'):
                    test.setFixture(self.fixture)
        unittest.TestSuite.run(self, result)
        self.tearDown()
        return result

    def _wrapped_run(self, *args, **kwargs):
        """Python 2.7 / unittest2 compatibility - there must be a better
        way..."""
        self.setUp()
        if hasattr(self, 'fixture'):
            for test in self._tests:
                if hasattr(test, 'setFixture'):
                    test.setFixture(self.fixture)
        unittest.TestSuite._wrapped_run(self, *args, **kwargs)
        self.tearDown()


class TestCaseSetup(unittest.TestCase):
    def setFixture(self, fixture):
        self.fixture = fixture


# -- Database utilities

def get_dburi():
    dburi = os.environ.get('TRAC_TEST_DB_URI')
    if dburi:
        scheme, db_prop = parse_connection_uri(dburi)
        # Assume the schema 'tractest' for PostgreSQL
        if scheme == 'postgres' and \
                not db_prop.get('params', {}).get('schema'):
            dburi += ('&' if '?' in dburi else '?') + 'schema=tractest'
        elif scheme == 'sqlite' and db_prop['path'] != ':memory:' and \
                not db_prop.get('params', {}).get('synchronous'):
            # Speed-up tests with SQLite database
            dburi += ('&' if '?' in dburi else '?') + 'synchronous=off'
        return dburi
    return 'sqlite::memory:'


def reset_sqlite_db(env, db_prop):
    """Deletes all data from the tables.

    :since 1.1.3: deprecated and will be removed in 1.3.1. Use `reset_tables`
                  from the database connection class instead.
    """
    return DatabaseManager(env).reset_tables()


def reset_postgres_db(env, db_prop):
    """Deletes all data from the tables and resets autoincrement indexes.

    :since 1.1.3: deprecated and will be removed in 1.3.1. Use `reset_tables`
                  from the database connection class instead.
    """
    return DatabaseManager(env).reset_tables()


def reset_mysql_db(env, db_prop):
    """Deletes all data from the tables and resets autoincrement indexes.

    :since 1.1.3: deprecated and will be removed in 1.3.1. Use `reset_tables`
                  from the database connection class instead.
    """
    return DatabaseManager(env).reset_tables()


class ConfigurationStub(Configuration):
    """A stub of the trac.config.Configuration class for testing."""

    def __init__(self, *args, **kwargs):
        super(ConfigurationStub, self).__init__(*args, **kwargs)
        self.file_content = []

    def _write(self, content):
        self.file_content = content


class EnvironmentStub(Environment):
    """A stub of the trac.env.Environment class for testing."""

    global_databasemanager = None
    required = False

    def __init__(self, default_data=False, enable=None, disable=None,
                 path=None, destroying=False):
        """Construct a new Environment stub object.

        :param default_data: If True, populate the database with some
                             defaults.
        :param enable: A list of component classes or name globs to
                       activate in the stub environment.
        :param disable: A list of component classes or name globs to
                        deactivate in the stub environment.
        :param path: The location of the environment in the file system.
                     No files or directories are created when specifying
                     this parameter.
        :param destroying: If True, the database will not be reset. This is
                           useful for cases when the object is being
                           constructed in order to call `destroy_db`.
        """
        if enable is not None and not isinstance(enable, (list, tuple)):
            raise TypeError('Keyword argument "enable" must be a list')
        if disable is not None and not isinstance(disable, (list, tuple)):
            raise TypeError('Keyword argument "disable" must be a list')

        ComponentManager.__init__(self)

        self.systeminfo = []

        import trac
        self.path = path
        if self.path is None:
            self.path = os.path.dirname(trac.__file__)
            if not os.path.isabs(self.path):
                self.path = os.path.join(os.getcwd(), self.path)

        # -- configuration
        self.config = ConfigurationStub(None)
        # We have to have a ticket-workflow config for ''lots'' of things to
        # work.  So insert the basic-workflow config here.  There may be a
        # better solution than this.
        load_workflow_config_snippet(self.config, 'basic-workflow.ini')
        self.config.set('logging', 'log_level', 'DEBUG')
        self.config.set('logging', 'log_type', 'stderr')
        if enable is not None:
            self.config.set('components', 'trac.*', 'disabled')
        else:
            self.config.set('components', 'tracopt.versioncontrol.*',
                            'enabled')
        for name_or_class in enable or ():
            config_key = self._component_name(name_or_class)
            self.config.set('components', config_key, 'enabled')
        for name_or_class in disable or ():
            config_key = self._component_name(name_or_class)
            self.config.set('components', config_key, 'disabled')

        # -- logging
        from trac.log import logger_handler_factory
        self.log, self._log_handler = logger_handler_factory('test')

        # -- database
        self.config.set('components', 'trac.db.*', 'enabled')
        self.dburi = get_dburi()

        init_global = False
        if self.global_databasemanager:
            self.components[DatabaseManager] = self.global_databasemanager
        else:
            self.config.set('trac', 'database', self.dburi)
            self.global_databasemanager = DatabaseManager(self)
            self.config.set('trac', 'debug_sql', True)
            init_global = not destroying

        if default_data or init_global:
            self.reset_db(default_data)

        self.config.set('trac', 'base_url', 'http://example.org/trac.cgi')

        translation.activate(locale_en)

    def reset_db(self, default_data=None):
        """Remove all data from Trac tables, keeping the tables themselves.

        :param default_data: after clean-up, initialize with default data
        :return: True upon success
        """
        from trac import db_default
        tables = []
        dbm = self.global_databasemanager
        try:
            with self.db_transaction as db:
                db.rollback()  # make sure there's no transaction in progress
                # check the database version
                db_version = dbm.get_database_version()
        except Exception:
            # "Database not found ...",
            # "OperationalError: no such table: system" or the like
            pass
        else:
            if db_version == db_default.db_version:
                # same version, simply clear the tables (faster)
                tables = dbm.reset_tables()
            else:
                # different version or version unknown, drop the tables
                dbm.destroy_db()

        if not tables:
            dbm.init_db()
            # we need to make sure the next get_db_cnx() will re-create
            # a new connection aware of the new data model - see #8518.
            if self.dburi != 'sqlite::memory:':
                dbm.shutdown()

        if default_data:
            dbm.insert_into_tables(db_default.get_data)
        else:
            dbm.set_database_version(db_default.db_version)

    def destroy_db(self, scheme=None, db_prop=None):
        """Destroy the database.

        :since 1.1.5: the method is deprecated and will be removed in 1.3.1.
                      Use `DatabaseManager.destroy_db` instead.
        """
        if not (scheme and db_prop):
            scheme, db_prop = parse_connection_uri(self.dburi)
        try:
            with self.db_transaction as db:
                if scheme == 'postgres' and db.schema:
                    db('DROP SCHEMA %s CASCADE' % db.quote(db.schema))
                elif scheme == 'mysql':
                    for table_name in db.get_table_names():
                        db.drop_table(table_name)
                elif scheme == 'sqlite':
                    path = db_prop['path']
                    if path != ':memory:':
                        if not os.path.isabs(path):
                            path = os.path.join(self.path, path)
                        self.global_databasemanager.shutdown()
                        os.remove(path)
        except Exception:
            # "TracError: Database not found...",
            # psycopg2.ProgrammingError: schema "tractest" does not exist
            pass
        return False

    def insert_known_users(self, users):
        with self.env.db_transaction as db:
            for username, name, email in users:
                db("INSERT INTO session VALUES (%s, %s, %s)",
                   (username, 1, int(time.time())))
                db("INSERT INTO session_attribute VALUES (%s,%s,'name',%s)",
                   (username, 1, name))
                db("INSERT INTO session_attribute VALUES (%s,%s,'email',%s)",
                   (username, 1, email))

    # overridden

    def is_component_enabled(self, cls):
        if self._component_name(cls).startswith('__main__.'):
            return True
        return Environment.is_component_enabled(self, cls)


def locate(fn):
    """Locates a binary on the path.

    Returns the fully-qualified path, or None.
    """
    exec_suffix = '.exe' if os.name == 'nt' else ''

    for p in ["."] + os.environ['PATH'].split(os.pathsep):
        f = os.path.join(p, fn + exec_suffix)
        if os.path.exists(f):
            return f
    return None


INCLUDE_FUNCTIONAL_TESTS = True


def suite():
    import trac.tests
    import trac.admin.tests
    import trac.db.tests
    import trac.mimeview.tests
    import trac.notification.tests
    import trac.timeline.tests
    import trac.ticket.tests
    import trac.util.tests
    import trac.versioncontrol.tests
    import trac.versioncontrol.web_ui.tests
    import trac.web.tests
    import trac.wiki.tests
    import tracopt.perm.tests
    import tracopt.ticket.tests
    import tracopt.versioncontrol.git.tests
    import tracopt.versioncontrol.svn.tests

    suite = unittest.TestSuite()
    suite.addTest(trac.tests.basicSuite())
    suite.addTest(trac.admin.tests.suite())
    suite.addTest(trac.db.tests.suite())
    suite.addTest(trac.mimeview.tests.suite())
    suite.addTest(trac.notification.tests.suite())
    suite.addTest(trac.ticket.tests.suite())
    suite.addTest(trac.timeline.tests.suite())
    suite.addTest(trac.util.tests.suite())
    suite.addTest(trac.versioncontrol.tests.suite())
    suite.addTest(trac.versioncontrol.web_ui.tests.suite())
    suite.addTest(trac.web.tests.suite())
    suite.addTest(trac.wiki.tests.suite())
    suite.addTest(tracopt.perm.tests.suite())
    suite.addTest(tracopt.ticket.tests.suite())
    suite.addTest(tracopt.versioncontrol.git.tests.suite())
    suite.addTest(tracopt.versioncontrol.svn.tests.suite())
    suite.addTest(doctest.DocTestSuite(sys.modules[__name__]))
    if INCLUDE_FUNCTIONAL_TESTS:
        suite.addTest(trac.tests.functionalSuite())
    return suite


if __name__ == '__main__':
    # FIXME: this is a bit inelegant
    if '--skip-functional-tests' in sys.argv:
        sys.argv.remove('--skip-functional-tests')
        INCLUDE_FUNCTIONAL_TESTS = False
    unittest.main(defaultTest='suite')
