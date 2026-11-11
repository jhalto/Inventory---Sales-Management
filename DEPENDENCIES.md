# Reproducible dependencies

requirements.txt pins the application and its transitive dependencies to the
versions installed in the tested Windows environment. .python-version records
CPython 3.14.6; installing Python itself is separate from pip. Other operating
systems or Python versions need their own clean-install validation.

The recorded pip version is 26.1.2. pip is build tooling, not an application
dependency, and is intentionally not included in requirements.txt.

Validated on 11 November 2026: a new .venv-dependency-check environment installed
all 11 pinned packages from requirements.txt, pip check reported no conflicts,
and all 74 tests passed on PostgreSQL with the shared-cache/schema runner. The
installed package set matched every exact pin; user-site packages were disabled
and the original virtual environment was absent from sys.path. The production
configuration subprocess check used this clean interpreter too. No live messages
were sent. The validation environment is ignored by Git and retained locally.

## Install in a fresh environment

After adding Google sign-in, the isolated environment was updated from the
complete pinned requirements and verified to contain exactly 22 matching runtime
packages. pip check passed and the full 88-test PostgreSQL/shared-cache suite
passed, including RSA verification and the production-profile subprocess check.
Google authentication dependencies and their transitives are pinned alongside
the original dependency set.

Run from the repository root with Python 3.14.6 available:

```powershell
python -m venv .venv-clean
.\.venv-clean\Scripts\python.exe -m pip install "pip==26.1.2"
.\.venv-clean\Scripts\python.exe -m pip install -r requirements.txt
.\.venv-clean\Scripts\python.exe -m pip check
.\.venv-clean\Scripts\python.exe -B manage.py test accounts api config.test_security --testrunner=config.postgres_test_runner.PostgresSchemaTestRunner
```

The test runner requires the local PostgreSQL connection from .env and CREATE
SCHEMA permission; it uses an isolated schema and disables live delivery. See
TESTING.md. Do not use --system-site-packages or copy packages from an existing
virtual environment when validating a clean install.

Exact versions constrain package selection, not artifact bytes. This file does
not provide hash verification. For artifact-level reproducibility, separately
maintain an approved platform-specific wheelhouse/hash manifest in your package
storage; do not commit virtual environments or database exports.

## Update process

1. Review dependency/security updates monthly and when an advisory affects the
   project. Use pip list --outdated in a disposable environment; do not upgrade
   the working environment automatically.
2. Choose the versions to update and review their official release notes,
   supported Python/Django versions and authentication behavior changes.
3. Resolve/install the proposed set in a disposable virtual environment. Update
   all affected exact pins, including changed transitive requirements. Do not
   blanket-upgrade everything as part of an unrelated change.
4. Recreate another clean environment from the edited requirements.txt, run
   pip check, the full PostgreSQL/shared-cache suite and production-profile
   deployment checks. Inspect migration changes before applying any.
5. Run real email/SMS smoke tests separately against explicitly authorized test
   recipients when delivery-related dependencies change; automated mocks do not
   verify provider delivery.
6. Commit dependency changes with the Python/pip/platform versions, validation
   results, relevant release-note links and any migration/rollback considerations.
   Keep the previous requirements file available through Git for rollback.
