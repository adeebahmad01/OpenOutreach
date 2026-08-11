import logging
import sys

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
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
        call_command("migrate", "--no-input")

        from openoutreach.core.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        from openoutreach.core.onboarding import missing_keys, onboard_interactive

        missing = missing_keys()
        if not missing:
            return

        if sys.stdin.isatty():
            onboard_interactive()
        else:
            self.stderr.write(
                f"Onboarding incomplete and no TTY available.\n"
                f"Missing: {', '.join(sorted(missing))}\n"
                f"Run with an interactive terminal to complete onboarding "
                f"(a mailbox and a BetterContact key must be connected)."
            )
            sys.exit(1)

    def _validate_operator(self):
        """Fail loudly on the three things the cycle cannot run without."""
        from openoutreach.core.models import SiteConfig
        from openoutreach.core.operator import campaigns, get_active_user

        if not SiteConfig.load().llm_api_key:
            logger.error("LLM_API_KEY is required. Set it in Site Configuration (Django Admin).")
            sys.exit(1)

        if get_active_user() is None:
            logger.error("No active operator account found.")
            sys.exit(1)

        if not campaigns():
            logger.error("No campaigns found for this operator.")
            sys.exit(1)
