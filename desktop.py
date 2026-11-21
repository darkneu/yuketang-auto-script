"""Native Windows desktop entry point. CLI remains available via main.py."""
if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    from yuketang.desktop import main
    raise SystemExit(main())
