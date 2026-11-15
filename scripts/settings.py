"""Secrets and settings for the DRISHTI scripts.

Lookup order for every key (first match wins):
  1. an environment variable with the same name
  2. the project's root .env file          (template: .env.example; .env is never committed)
  3. a legacy per-script file, e.g. scripts/gemini.env or scripts/meteomatics.env (still honoured)
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ROOT_ENV = os.path.join(ROOT, ".env")


def read_env_file(path):
    """KEY=VALUE lines; '#' comments and blank lines ignored; surrounding quotes stripped. Missing file -> {}."""
    out = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_settings(names, legacy=None, root_env=ROOT_ENV):
    """{name: value} for the requested names; a name with no value anywhere is left out."""
    sources = [read_env_file(legacy) if legacy else {}, read_env_file(root_env)]   # later entries win
    found = {}
    for src in sources:
        found.update({k: v for k, v in src.items() if k in names and v})
    for k in names:
        if os.environ.get(k):
            found[k] = os.environ[k]
    return found


def meteomatics_auth():
    """(user, password) for the Meteomatics API, or exit with a clear message."""
    s = get_settings(["METEOMATICS_USER", "METEOMATICS_PASSWORD"], legacy=os.path.join(HERE, "meteomatics.env"))
    if not s.get("METEOMATICS_USER") or not s.get("METEOMATICS_PASSWORD"):
        raise SystemExit("Meteomatics login missing: set METEOMATICS_USER and METEOMATICS_PASSWORD in .env "
                         "(see .env.example) or as environment variables.")
    return s["METEOMATICS_USER"], s["METEOMATICS_PASSWORD"]
