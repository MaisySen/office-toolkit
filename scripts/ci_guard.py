"""Guard for the CI only Keycloak seeding scripts.

ci_runtime_stack.py and keycloak_ci_journey.py create the toolkit clients
with the password grant (direct access grants) switched on and fixed test
secrets and users, so CI can mint tokens without a browser. That shape must
never reach a real realm: production clients are provisioned by
provision_keycloak.py, which switches the password grant off and verifies
it. These scripts therefore refuse to run outside CI (GitHub Actions, or an
explicit TOOLKIT_ALLOW_CI_SEED=1 for a local reproduction) and refuse any
Keycloak or issuer URL that is not a plain http loopback or emulator alias.
"""
import os
from urllib.parse import urlsplit

CI_HOSTS = ("localhost", "127.0.0.1", "10.0.2.2")


def ci_seed_problems(urls, env=None):
    env = os.environ if env is None else env
    problems = []
    if (env.get("GITHUB_ACTIONS") != "true"
            and env.get("TOOLKIT_ALLOW_CI_SEED") != "1"):
        problems.append("not running in CI (set TOOLKIT_ALLOW_CI_SEED=1 "
                        "only for a throwaway local Keycloak)")
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme != "http" or (parts.hostname or "") not in CI_HOSTS:
            problems.append(f"{url} is not a CI loopback address")
    return problems


def assert_ci_only(*urls):
    problems = ci_seed_problems(urls)
    if problems:
        raise SystemExit("CI-SEED-REFUSED: " + "; ".join(problems))
