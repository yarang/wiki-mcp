"""Enable `python -m wiki_mcp` execution — MCP 서버 또는 CLI(kb 하위 명령)."""

import sys

from wiki_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
