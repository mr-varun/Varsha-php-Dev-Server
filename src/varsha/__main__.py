"""Allow `python -m varsha`. The frozen Varsha.exe starts here too.

A frozen exe can't do `python -m <module>`, so it runs its own helper modules (the DNS service,
megha) as `Varsha.exe --run-module varsha.<module> [args]`. Only varsha's own modules are allowed.
"""

import sys

RUN_MODULE = "--run-module"


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--version":
        from varsha import __version__
        from varsha.paths import find_root

        print(f"varsha {__version__}\nroot {find_root()}")
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == RUN_MODULE:
        module = sys.argv[2] if len(sys.argv) >= 3 else ""
        if not module.startswith("varsha.") or not all(part.isidentifier() for part in module.split(".")):
            print(f"varsha: {RUN_MODULE} needs a varsha.<module> name, got {module!r}", file=sys.stderr)
            return 2
        import runpy

        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    from varsha.gui.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
