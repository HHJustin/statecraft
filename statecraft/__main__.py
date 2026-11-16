"""`python -m statecraft ...` 的入口。

只做一件事：把参数交给 `statecraft.cli.main`，并把返回码交给 shell。
"""
import sys

from statecraft.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
