"""`python -m agent_desk` entry point."""
import sys

from . import desk, setup


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("init", "configure-dispatcher", "doctor"):
        return setup.main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "dispatch":
        from . import dispatcher
        return dispatcher.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from . import web
        return web.main(sys.argv[2:])
    return desk.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
