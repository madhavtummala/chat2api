"""A tiny MCP server used by tests (stdio transport). Run: python mock_mcp_server.py"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mock")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    mcp.run()  # defaults to stdio transport
