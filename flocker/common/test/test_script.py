# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""Tests for :module:`flocker.common.script`."""

import sys
import os

from zope.interface.verify import verifyObject

from eliot.testing import validateLogging, assertHasMessage

from appdirs import AppDirs

from twisted.application.service import IService
from twisted.internet import task
from twisted.internet.defer import succeed
from twisted.python import usage
from twisted.trial.unittest import SynchronousTestCase
from twisted.python.failure import Failure
from twisted.python.log import LogPublisher
from twisted.python import log as twisted_log
from twisted.python.filepath import FilePath
from twisted.internet.defer import Deferred
from twisted.application.service import Service
from twisted.python.usage import Options

# XXX: We shouldn't be using this private fake and Twisted probably
# shouldn't either. See https://twistedmatrix.com/trac/ticket/6200 and
# https://twistedmatrix.com/trac/ticket/7527
from twisted.test.test_task import _FakeReactor

from ..script import (
    FlockerScriptRunner, main_for_service,
    EliotObserver, TWISTED_LOG_MESSAGE,
    _flocker_standard_options,
    ILoggingPolicy,
    StdoutLoggingPolicy,
    NullLoggingPolicy,
    CLILoggingPolicy,
    )
from ...testtools import (
    help_problems, FakeSysModule,
    MemoryCoreReactor,
    )
from ... import __version__


def make_ilogging_policy_tests(fixture):
    """
    Create tests for :class:`ILoggingPolicy` providers.

    :param fixture: Function that creates an :class:`ILoggingPolicy` provider.
    """

    class ILoggingPolicyTests(SynchronousTestCase):
        """
        Tests for :class:`ILoggingPolicy` providers.
        """
        def test_interface(self):
            """
            The logging policy provides :class:`ILoggingPolicy`.
            """
            verifyObject(ILoggingPolicy, fixture(self))

        def test_service(self):
            """
            :class:`ILoggingPolicy.service` returns an object that provides
            :class:`IService`.
            """
            policy = fixture(self)
            options = policy.options_wrapper(TestOptions)()
            verifyObject(IService, policy.service(_FakeReactor(), options))

        def test_options(self):
            """
            :class:`ILoggingPolicy.options_wrapper` returns a subclass of
            the :class:`Options` subclass passed to it.
            """
            self.assertIsInstance(
                fixture(self).options_wrapper(TestOptions)(),
                TestOptions)

    return ILoggingPolicyTests


class StdoutLoggingPolicyTests(
        make_ilogging_policy_tests(lambda self: StdoutLoggingPolicy())):
    """
    Tests for :class:`StdoutLoggingPolicy`.
    """

    def test_sys_default(self):
        """
        `StdoutLoggingPolicy.sys_module` is `sys` by default.
        """
        self.assertIs(
            sys,
            StdoutLoggingPolicy().sys_module)

    def test_sys_override(self):
        """
        `StdoutLoggingPolicy.sys_module` can be overridden in the constructor.
        """
        dummy_sys = object()
        self.assertIs(
            dummy_sys,
            StdoutLoggingPolicy(sys_module=dummy_sys).sys_module
        )


class NullLoggingPolicyTests(
        make_ilogging_policy_tests(lambda self: NullLoggingPolicy())):
    """
    Tests for :class:`NullLoggingPolicy`.
    """


class FakeAppDirs(object):
    """
    Fake implementation of :class:`appdirs.Appdirs`.

    :ivar bytes user_log_dir: The directory where logs for this application
        should be written.
    """
    def __init__(self, case):
        """
        :param case: Test case to use for creating temporary directory.
        """
        log_dir = FilePath(case.mktemp())
        log_dir.createDirectory()
        self.user_log_dir = log_dir.path


class CLILoggingPolicyTests(
        make_ilogging_policy_tests(
            lambda self: CLILoggingPolicy(appdirs=FakeAppDirs(self)))):
    """
    Tests for :class:`CLILoggingPolicy`.
    """

    def test_appdirs_default(self):
        """
        `CLILoggingPolicy`s default log-dir is from ``AppDirs.user_log_dir``.
        """
        policy = CLILoggingPolicy()
        options = policy.options_wrapper(TestOptions)()
        self.assertEqual(
            AppDirs("Flocker", "ClusterHQ").user_log_dir,
            options['log-dir'])

    def test_appdirs_override(self):
        """
        `CLILoggingPolicy`s default log-dir can be overridden in the
        constructor.
        """
        appdirs = FakeAppDirs(self)
        policy = CLILoggingPolicy(appdirs=appdirs)
        options = policy.options_wrapper(TestOptions)()
        self.assertIs(
            appdirs.user_log_dir,
            options['log-dir'])

    def test_getpid_default(self):
        """
        `CLILoggingPolicy._getpid` is `os.getpid` by default.
        """
        policy = CLILoggingPolicy()
        self.assertEqual(
            os.getpid, policy._getpid)

    def test_getpid_override(self):
        """
        `CLILoggingPolicy._getpid` can be overridden in the constructor.
        """
        def getpid():
            return 5
        policy = CLILoggingPolicy(_getpid=getpid)
        self.assertEqual(
            getpid, policy._getpid)


class FlockerScriptRunnerInitTests(SynchronousTestCase):
    """Tests for :py:meth:`FlockerScriptRunner.__init__`."""

    def test_sys_default(self):
        """
        `FlockerScriptRunner.sys` is `sys` by default.
        """
        self.assertIs(
            sys,
            FlockerScriptRunner(
                script=None, options=Options,
                logging_policy=NullLoggingPolicy(),
                ).sys_module
        )

    def test_sys_override(self):
        """
        `FlockerScriptRunner.sys` can be overridden in the constructor.
        """
        dummySys = object()
        self.assertIs(
            dummySys,
            FlockerScriptRunner(script=None, options=Options,
                                logging_policy=NullLoggingPolicy(),
                                sys_module=dummySys).sys_module
        )

    def test_react(self):
        """
        `FlockerScriptRunner._react` is ``task.react`` by default
        """
        self.assertIs(
            task.react,
            FlockerScriptRunner(script=None, options=Options,
                                logging_policy=NullLoggingPolicy(),
                                )._react
        )

    def test_logging_policy(self):
        """
        `FlockerScriptRunner.logging_policy` can be set in the constructor.
        """
        logging_policy = NullLoggingPolicy()
        self.assertIs(
            logging_policy,
            FlockerScriptRunner(script=None, options=Options,
                                logging_policy=logging_policy,
                                ).logging_policy
        )


class FlockerScriptRunnerParseOptionsTests(SynchronousTestCase):
    """Tests for :py:meth:`FlockerScriptRunner._parse_options`."""

    def test_parse_options(self):
        """
        ``FlockerScriptRunner._parse_options`` accepts a list of arguments,
        passes them to the `parseOptions` method of its ``options`` attribute
        and returns the populated options instance.
        """
        class OptionsSpy(usage.Options):
            def parseOptions(self, arguments):
                self.parseOptionsArguments = arguments

        expectedArguments = [object(), object()]
        runner = FlockerScriptRunner(script=None, options=OptionsSpy,
                                     logging_policy=NullLoggingPolicy())
        options = runner._parse_options(expectedArguments)
        self.assertEqual(expectedArguments, options.parseOptionsArguments)

    def test_parse_options_usage_error(self):
        """
        `FlockerScriptRunner._parse_options` catches `usage.UsageError`
        exceptions and writes the help text and an error message to `stderr`
        before exiting with status 1.
        """
        expectedMessage = b'foo bar baz'
        expectedCommandName = b'test_command'

        class FakeOptions(usage.Options):
            synopsis = 'Usage: %s [options]' % (expectedCommandName,)

            def parseOptions(self, arguments):
                raise usage.UsageError(expectedMessage)

        fake_sys = FakeSysModule()

        runner = FlockerScriptRunner(script=None, options=FakeOptions,
                                     sys_module=fake_sys,
                                     logging_policy=NullLoggingPolicy())
        error = self.assertRaises(SystemExit, runner._parse_options, [])
        expectedErrorMessage = b'ERROR: %s\n' % (expectedMessage,)
        errorText = fake_sys.stderr.getvalue()
        self.assertEqual(
            (1, [], expectedErrorMessage),
            (error.code,
             help_problems(u'test_command', errorText),
             errorText[-len(expectedErrorMessage):])
        )


class FlockerScriptRunnerMainTests(SynchronousTestCase):
    """Tests for :py:meth:`FlockerScriptRunner.main`."""

    def test_main_uses_sysargv(self):
        """
        ``FlockerScriptRunner.main`` uses ``self.sys_module.argv``.
        """
        class SpyOptions(usage.Options):
            def opt_hello(self, value):
                self.value = value

        class SpyScript(object):
            def main(self, reactor, arguments):
                self.reactor = reactor
                self.arguments = arguments
                return succeed(None)

        options = SpyOptions
        script = SpyScript()
        sys = FakeSysModule(argv=[b"flocker", b"--hello", b"world"])
        fakeReactor = _FakeReactor()
        runner = FlockerScriptRunner(script, options,
                                     reactor=fakeReactor, sys_module=sys,
                                     logging_policy=NullLoggingPolicy())
        self.assertRaises(SystemExit, runner.main)
        self.assertEqual(b"world", script.arguments.value)

    def test_disabled_logging(self):
        """
        If ``logging`` is set to ``False``, ``FlockerScriptRunner.main``
        does not log to ``sys.stdout``.
        """
        class Script(object):
            def main(self, reactor, arguments):
                twisted_log.msg(b"hello!")
                return succeed(None)

        script = Script()
        sys = FakeSysModule(argv=[])
        fakeReactor = _FakeReactor()
        runner = FlockerScriptRunner(script, usage.Options,
                                     reactor=fakeReactor, sys_module=sys,
                                     logging_policy=NullLoggingPolicy())
        self.assertRaises(SystemExit, runner.main)
        self.assertEqual(sys.stdout.getvalue(), b"")


class TestOptions(usage.Options):
    """An unmodified ``usage.Options`` subclass for use in testing."""


class FlockerStandardOptionsTests(SynchronousTestCase):
    """Tests for ``_flocker_standard_options``

    Using a decorating an unmodified ``usage.Options`` subclass.
    """
    options = _flocker_standard_options(TestOptions)

    def test_sys_module_default(self):
        """
        ``flocker_standard_options`` adds a ``_sys_module`` attribute which is
        ``sys`` by default.
        """
        self.assertIs(sys, self.options()._sys_module)

    def test_sys_module_override(self):
        """
        ``flocker_standard_options`` adds a ``sys_module`` argument to the
        initialiser which is assigned to ``_sys_module``.
        """
        dummy_sys_module = object()
        self.assertIs(
            dummy_sys_module,
            self.options(sys_module=dummy_sys_module)._sys_module
        )

    def test_version(self):
        """
        Flocker commands have a `--version` option which prints the current
        version string to stdout and causes the command to exit with status
        `0`.
        """
        sys = FakeSysModule()
        error = self.assertRaises(
            SystemExit,
            self.options(sys_module=sys).parseOptions,
            ['--version']
        )
        self.assertEqual(
            (__version__ + '\n', 0),
            (sys.stdout.getvalue(), error.code)
        )


class AsyncStopService(Service):
    """
    An ``IService`` implementation which can return an unfired ``Deferred``
    from its ``stopService`` method.

    :ivar Deferred stop_result: The object to return from ``stopService``.
        ``AsyncStopService`` won't do anything more than return it.  If it is
        ever going to fire, some external code is responsible for firing it.
    """
    def __init__(self, stop_result):
        self.stop_result = stop_result

    def stopService(self):
        Service.stopService(self)
        return self.stop_result


class MainForServiceTests(SynchronousTestCase):
    """
    Tests for ``main_for_service``.
    """
    def setUp(self):
        self.reactor = MemoryCoreReactor()
        self.service = Service()

    def _shutdown_reactor(self, reactor):
        """
        Simulate reactor shutdown.

        :param IReactorCore reactor: The reactor to shut down.
        """
        reactor.fireSystemEvent("shutdown")

    def test_starts_service(self):
        """
        ``main_for_service`` accepts an ``IService`` provider and starts it.
        """
        main_for_service(self.reactor, self.service)
        self.assertTrue(
            self.service.running, "The service should have been started.")

    def test_returns_unfired_deferred(self):
        """
        ``main_for_service`` returns a ``Deferred`` which has not fired.
        """
        result = main_for_service(self.reactor, self.service)
        self.assertNoResult(result)

    def test_fire_on_stop(self):
        """
        The ``Deferred`` returned by ``main_for_service`` fires with ``None``
        when the reactor is stopped.
        """
        result = main_for_service(self.reactor, self.service)
        self._shutdown_reactor(self.reactor)
        self.assertIs(None, self.successResultOf(result))

    def test_stops_service(self):
        """
        When the reactor is stopped, ``main_for_service`` stops the service it
        was called with.
        """
        main_for_service(self.reactor, self.service)
        self._shutdown_reactor(self.reactor)
        self.assertFalse(
            self.service.running, "The service should have been stopped.")

    def test_wait_for_service_stop(self):
        """
        The ``Deferred`` returned by ``main_for_service`` does not fire before
        the ``Deferred`` returned by the service's ``stopService`` method
        fires.
        """
        result = main_for_service(self.reactor, AsyncStopService(Deferred()))
        self._shutdown_reactor(self.reactor)
        self.assertNoResult(result)

    def test_fire_after_service_stop(self):
        """
        The ``Deferred`` returned by ``main_for_service`` fires once the
        ``Deferred`` returned by the service's ``stopService`` method fires.
        """
        async = Deferred()
        result = main_for_service(self.reactor, AsyncStopService(async))
        self._shutdown_reactor(self.reactor)
        async.callback(None)
        self.assertIs(None, self.successResultOf(result))


class EliotObserverTests(SynchronousTestCase):
    """
    Tests for ``EliotObserver``.
    """
    @validateLogging(None)
    def test_message(self, logger):
        """
        A message logged to the given ``LogPublisher`` is converted to an
        Eliot log message.
        """
        publisher = LogPublisher()
        observer = EliotObserver(publisher)
        observer.logger = logger
        publisher.addObserver(observer)
        publisher.msg(b"Hello", b"world")
        assertHasMessage(self, logger, TWISTED_LOG_MESSAGE,
                         dict(error=False, message=u"Hello world"))

    @validateLogging(None)
    def test_error(self, logger):
        """
        An error logged to the given ``LogPublisher`` is converted to an Eliot
        log message.
        """
        publisher = LogPublisher()
        observer = EliotObserver(publisher)
        observer.logger = logger
        publisher.addObserver(observer)
        # No public API for this unfortunately, so emulate error logging:
        publisher.msg(failure=Failure(ZeroDivisionError("onoes")),
                      why=b"A zero division ono",
                      isError=True)
        message = (u'A zero division ono\nTraceback (most recent call '
                   u'last):\nFailure: exceptions.ZeroDivisionError: onoes\n')
        assertHasMessage(self, logger, TWISTED_LOG_MESSAGE,
                         dict(error=True, message=message))
