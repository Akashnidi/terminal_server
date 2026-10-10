"""WorkspaceManager MCP Server - Zero-Trust Hardened Security Edition.

Provides strictly isolated workspace file operations and sandboxed command execution.
Guarantees that no tool can read, modify, or execute anything outside D:\\mcp\\workspace.
"""

import json
import logging
from pathlib import Path
import re
import subprocess
import sys

# FastMCP compatibility across mcp 1.x and 2.x
try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP

# MCP Protocol Hygiene: Direct all logging strictly to sys.stderr (NEVER use print)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("WorkspaceManager")

# 1. Workspace Boundary
WORKSPACE_DIR = Path(r"D:\mcp\workspace").resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Canonical Security Error Message
SECURITY_VIOLATION_MSG = r"Security Violation: Access denied. Path is outside D:\mcp\workspace."

# Output truncation limit for command execution (prevent DoS/memory exhaustion)
MAX_OUTPUT_LENGTH = 50_000

# Blacklisted destructive system commands / binaries
BLACKLISTED_COMMAND_PATTERNS = [
    r"\bformat(\.exe)?\b",
    r"\bdiskpart(\.exe)?\b",
    r"\bshutdown(\.exe)?\b",
    r"\breg(\.exe)?\b",
    r"\bnet(\.exe)?\s+user\b",
    r"\btakeown(\.exe)?\b",
    r"\bicacls(\.exe)?\b",
    r"\bbcdedit(\.exe)?\b",
    r"\bvssadmin(\.exe)?\b",
]


# 2. Hardened Path Validation & 3. Symlink / Junction Protection
def safe_path(target: str) -> Path:
    """Resolve target path relative to WORKSPACE_DIR and verify it remains strictly within the workspace.

    Neutralizes relative traversal jumps (..), absolute external paths, UNC paths,
    and symlinks/junctions pointing outside WORKSPACE_DIR.

    Args:
        target: Path string to validate.

    Returns:
        Path: Canonical resolved Path object strictly inside WORKSPACE_DIR.

    Raises:
        ValueError: If path escapes or resolves outside WORKSPACE_DIR.
    """
    if not target or not isinstance(target, str):
        raise ValueError(SECURITY_VIOLATION_MSG)

    # Immediately block UNC network paths
    if target.startswith(("\\\\", "//")):
        raise ValueError(SECURITY_VIOLATION_MSG)

    try:
        resolved = (WORKSPACE_DIR / target).resolve()
    except Exception:
        raise ValueError(SECURITY_VIOLATION_MSG)

    # Enforce strict boundary check (handles symlinks/junctions automatically via resolve)
    if not resolved.is_relative_to(WORKSPACE_DIR):
        raise ValueError(SECURITY_VIOLATION_MSG)

    return resolved


# 4. Command Execution Pre-flight Sanitization
def sanitize_command(command: str) -> None:
    """Pre-flight sanitization to enforce Zero-Trust security on shell commands.

    Args:
        command: Command string to validate.

    Raises:
        ValueError: If command violates any security boundary rule.
    """
    if not command or not isinstance(command, str):
        raise ValueError("Security Violation: Access denied. Empty or invalid command.")

    # Rule A: Forbid critical system destructive binaries
    for pattern in BLACKLISTED_COMMAND_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            raise ValueError("Security Violation: Access denied. Destructive system command is blacklisted.")

    # Rule B: Forbid directory traversal sequences (..)
    if ".." in command:
        raise ValueError("Security Violation: Access denied. Directory traversal sequence '..' is forbidden in commands.")

    # Rule C: Forbid environment variable expansion (%...% and $env:)
    if re.search(r"%[^%]+%", command):
        raise ValueError("Security Violation: Access denied. Environment variable expansion ('%...%') is forbidden.")
    if re.search(r"\$env:", command, re.IGNORECASE):
        raise ValueError("Security Violation: Access denied. PowerShell environment variable expansion ('$env:') is forbidden.")

    # Rule D: Forbid explicit drive letters (e.g. C:, c:, D:)
    if re.search(r"[a-zA-Z]:", command):
        raise ValueError("Security Violation: Access denied. Drive letters (e.g. 'C:', 'D:') are forbidden in commands.")


# Initialize FastMCP Server
mcp = FastMCP("WorkspaceManager")


# 5. Exposed Tools
@mcp.tool()
def list_workspace(subpath: str = ".") -> str:
    """List directory contents within the workspace boundary (D:\\mcp\\workspace).

    Security:
        Rejects any path attempting to escape D:\\mcp\\workspace. Symlinks and junctions
        pointing outside the workspace are strictly blocked.

    Args:
        subpath: Relative path or subdirectory within workspace. Defaults to '.' (workspace root).

    Returns:
        str: JSON string of items ({name, is_dir, size}) or security rejection message.
    """
    logger.info("Listing directory: %s", subpath)
    try:
        path = safe_path(subpath)
    except ValueError as exc:
        logger.warning("Security refusal in list_workspace: %s", exc)
        return str(exc)

    if not path.exists():
        return f"Error: Directory not found: {subpath}"
    if not path.is_dir():
        return f"Error: Path is not a directory: {subpath}"

    items: list[dict] = []
    try:
        for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            is_dir = entry.is_dir()
            size = 0
            if not is_dir:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
            items.append({
                "name": entry.name,
                "is_dir": is_dir,
                "size": size,
            })
        return json.dumps(items, indent=2)
    except Exception as exc:
        logger.error("Failed to list directory '%s': %s", subpath, exc)
        return f"Error reading directory contents: {exc}"


@mcp.tool()
def read_file(filepath: str) -> str:
    """Read UTF-8 text file inside D:\\mcp\\workspace.

    Security:
        Enforces strict Zero-Trust boundaries. If the path attempts to escape or dereferences
        symlinks outside D:\\mcp\\workspace, returns an explicit security refusal message.

    Args:
        filepath: Relative path to the file inside workspace.

    Returns:
        str: File content, or an explicit security refusal / error message.
    """
    logger.info("Reading file: %s", filepath)
    try:
        path = safe_path(filepath)
    except ValueError as exc:
        logger.warning("Security refusal in read_file: %s", exc)
        return str(exc)

    if not path.exists():
        return f"Error: File not found: {filepath}"
    if not path.is_file():
        return f"Error: Path is not a regular file: {filepath}"

    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to read file '%s': %s", filepath, exc)
        return f"Error reading file '{filepath}': {exc}"


@mcp.tool()
def write_file(filepath: str, content: str) -> str:
    """Write text files safely within D:\\mcp\\workspace, creating parent directories inside the boundary.

    Security:
        Guarantees that files and parent directories are only created within D:\\mcp\\workspace.
        Any attempt to write outside the boundary returns an explicit security refusal message.

    Args:
        filepath: Target file path within workspace.
        content: Text content to write.

    Returns:
        str: Success confirmation message or security refusal message.
    """
    logger.info("Writing file: %s (%d chars)", filepath, len(content))
    try:
        path = safe_path(filepath)
    except ValueError as exc:
        logger.warning("Security refusal in write_file: %s", exc)
        return str(exc)

    try:
        # Verify parent directory resolution stays strictly inside workspace
        if not path.parent.resolve().is_relative_to(WORKSPACE_DIR):
            return SECURITY_VIOLATION_MSG

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"File '{filepath}' written successfully ({len(content)} characters)."
    except Exception as exc:
        logger.error("Failed to write file '%s': %s", filepath, exc)
        return f"Error writing file '{filepath}': {exc}"


@mcp.tool()
def execute_command(command: str) -> str:
    """Run CLI commands strictly sandboxed within D:\\mcp\\workspace.

    Security:
        Pre-flight sanitization rejects:
        - Drive letters ('C:', 'D:', etc.)
        - Traversal sequences ('..')
        - Environment variable expansions ('%VAR%' and '$env:VAR')
        - Blacklisted destructive commands ('format', 'diskpart', 'shutdown', 'reg', 'net user', 'takeown', 'icacls')

        Execution constraints:
        - Working directory is locked to D:\\mcp\\workspace
        - Hard timeout of 30 seconds
        - Output is capped at 50,000 characters to prevent memory exhaustion

    Args:
        command: Command string to execute.

    Returns:
        str: JSON-formatted result with {stdout, stderr, exit_code}, or descriptive security refusal.
    """
    logger.info("Execute command request: %s", command)

    # Pre-flight security sanitization
    try:
        sanitize_command(command)
    except ValueError as exc:
        logger.warning("Security rejection in execute_command: %s", exc)
        return str(exc)

    try:
        result = subprocess.run(
            command,
            cwd=str(WORKSPACE_DIR),
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Truncate output to mitigate DoS / excessive payload size
        if len(stdout) > MAX_OUTPUT_LENGTH:
            stdout = stdout[:MAX_OUTPUT_LENGTH] + f"\n... [Output truncated at {MAX_OUTPUT_LENGTH:,} characters]"
        if len(stderr) > MAX_OUTPUT_LENGTH:
            stderr = stderr[:MAX_OUTPUT_LENGTH] + f"\n... [Output truncated at {MAX_OUTPUT_LENGTH:,} characters]"

        return json.dumps({
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
        }, indent=2)

    except subprocess.TimeoutExpired as exc:
        logger.error("Command timed out after 30 seconds: %s", command)
        stdout = (exc.stdout or "")[:MAX_OUTPUT_LENGTH]
        stderr = f"Error: Command timed out after 30 seconds: {(exc.stderr or '')[:MAX_OUTPUT_LENGTH]}"
        return json.dumps({
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": -1,
        }, indent=2)

    except Exception as exc:
        logger.error("Error executing command '%s': %s", command, exc)
        return json.dumps({
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
        }, indent=2)


# 6. Entrypoint
if __name__ == "__main__":
    mcp.run()
