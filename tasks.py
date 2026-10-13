"""Cross-platform task runner (no `make`).

    python tasks.py up         # start Postgres via docker compose, create tables, seed
    python tasks.py test       # run pytest (needs Postgres reachable at TEST_DATABASE_URL)
    python tasks.py demo       # run scripts/demo.py end to end with the configured provider
    python tasks.py calibrate  # LLM-vs-expected score deltas on the golden set
    python tasks.py cost       # token/cost report + 30 and 10,000/day projection
"""
import subprocess
import sys


def _run(*args: str) -> int:
    return subprocess.call([sys.executable, *args])


def up() -> int:
    code = subprocess.call(["docker", "compose", "up", "-d", "postgres"])
    if code:
        return code
    return _run("-m", "app.bootstrap")


def test() -> int:
    return _run("-m", "pytest", *sys.argv[2:])


def demo() -> int:
    return _run("scripts/demo.py")


def calibrate() -> int:
    return _run("-m", "app.calibrate")


def cost() -> int:
    return _run("-m", "app.cost")


COMMANDS = {"up": up, "test": test, "demo": demo, "calibrate": calibrate, "cost": cost}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]]())
