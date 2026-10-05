"""Make `python3 -m stage1.harvest ...` resolve to the harvest runner,
mirroring `python3 -m stage1.run`."""

from stage1.harvest.run import main

if __name__ == "__main__":
    raise SystemExit(main())
