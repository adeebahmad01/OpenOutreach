import logging
import sys

from django.core.management import call_command

from openoutreach.core.errors import ErrorType, OpenOutreachError
from openoutreach.core.management.base import OpenOutreachCommand

logger = logging.getLogger(__name__)


class Command(OpenOutreachCommand):
    help = "Run the OpenOutreach daemon (onboard, validate, start the cycle)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--log-level",
            choices=("debug", "info", "warning", "error"),
            help="Log verbosity (default: info). `debug` shows the discovery walk's "
                 "reasoning — the frontier, each node's counts and draw, why a node "
                 "was expanded or not, and the provider's raw answer.",
        )

    def handle(self, *args, **options):
        self._configure_logging(options.get("log_level"), options["verbosity"])
        self._ensure_db()
        self._ensure_onboarded()
        self._validate_operator()

        from openoutreach.core.cycle import run_daemon
        run_daemon()

    # -- Steps ---------------------------------------------------------------

    def _configure_logging(self, log_level: str | None, verbosity: int):
        """``--log-level`` wins; Django's ``-v 2`` stays as the shorthand for debug."""
        from openoutreach.core.logging import configure_logging, print_banner

        if log_level:
            level = getattr(logging, log_level.upper())
        else:
            level = logging.DEBUG if verbosity >= 2 else logging.INFO
        configure_logging(level=level)
        print_banner()

    def _ensure_db(self):
        # Django's migrate narrates to stdout. The daemon has no result, so all of its
        # output is progress — and a stray "Applying core.0001_initial… OK" in a
        # redirected stdout is exactly what the contract exists to prevent.
        call_command("migrate", "--no-input", stdout=self.stderr)

        from openoutreach.core.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        """Environment first, wizard only if a human is there to answer.

        The order is the point: an agent-driven install has no TTY, so the
        non-interactive path is the main path. What the environment cannot satisfy
        goes to the wizard on a TTY, or exits **naming the variables** that would
        have satisfied it — never a bare "onboarding incomplete".
        """
        from openoutreach.core import onboarding

        if not onboarding.missing_keys():
            return

        filled = onboarding.hydrate_from_env()
        if filled:
            logger.info("Configured from the environment: %s.", ", ".join(sorted(filled)))
        if not onboarding.missing_keys():
            return

        if sys.stdin.isatty():
            onboarding.onboard_interactive()
            return

        raise OpenOutreachError(
            ErrorType.ONBOARDING_INCOMPLETE,
            "no TTY, and the environment does not carry everything.\n"
            "Set these and run again:\n"
            f"{onboarding.env_help()}\n"
            "Optional: "
            f"{onboarding.ENV_PREFIX}CAMPAIGN_NAME, {onboarding.ENV_PREFIX}LLM_API_BASE "
            f"(required for openai_compatible:*), {onboarding.ENV_PREFIX}NEWSLETTER.\n"
            f"{onboarding.ENV_PREFIX}ACCEPT_LEGAL_NOTICE must be set to 'true' — it "
            f"records that you accept {onboarding.LEGAL_NOTICE_URL}.",
        )

    def _validate_operator(self):
        """Fail loudly on the three things the cycle cannot run without.

        Each exits with a typed line rather than a log record: these are answers to
        the reader, and a program needs to branch on them.
        """
        from openoutreach.core.models import SiteConfig
        from openoutreach.core.operator import campaigns, get_active_user

        if not SiteConfig.load().llm_api_key:
            raise OpenOutreachError(
                ErrorType.ONBOARDING_INCOMPLETE,
                "no LLM API key — set OPENOUTREACH_LLM_API_KEY, or edit Site "
                "Configuration in the Django Admin.",
            )

        if get_active_user() is None:
            raise OpenOutreachError(
                ErrorType.ONBOARDING_INCOMPLETE, "no active operator account.")

        if not campaigns():
            raise OpenOutreachError(
                ErrorType.ONBOARDING_INCOMPLETE, "no campaigns for this operator.")
