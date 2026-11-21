"""Claude CLI wrapper for the Telegram Business Assistant Bot."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ClaudeError(Exception):
    """Base Claude CLI error."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeTimeout(ClaudeError):
    """Claude CLI timeout error."""
    pass


class ClaudeAuthError(ClaudeError):
    """Claude authentication error."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message, detail)


class ClaudeLimitError(ClaudeError):
    """Claude usage limit error."""

    def __init__(
        self,
        detail: str,
        resets_at: float | None = None,
        reset_text: str = "",
    ) -> None:
        message = f"Claude subscription usage limit reached (resets {reset_text or 'unknown'})"
        super().__init__(message, detail)
        self.resets_at = resets_at
        self.reset_text = reset_text


def _redact_token(text: str, token: str | None) -> str:
    """Redact token from text."""
    if not token:
        return text
    return text.replace(token, "[REDACTED]")


def _extract_epoch(text: str) -> float | None:
    """Extract epoch timestamp or parse reset time from text."""
    # Try epoch format |1760000000|
    match = re.search(r"\|(\d{10,})\|", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Try ISO date format like 2024-01-01T12:00:00Z
    match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", text)
    if match:
        try:
            from datetime import datetime, timezone
            iso_str = match.group(0).replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass

    # Try "retry after X seconds"
    match = re.search(r"(?:retry|reset)\s+after\s+(\d+)\s*s", text, re.IGNORECASE)
    if match:
        import time
        return time.time() + float(match.group(1))

    return None


def _extract_reset_text(text: str) -> str:
    """Extract human-readable reset time from text."""
    match = re.search(r"resets?\s+([^|\n]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _clean_detail(text: str, epoch: float | None) -> str:
    """Clean detail text by removing epoch marker."""
    if epoch is not None:
        text = text.replace(f"|{int(epoch)}|", "")
    return text.strip()


def classify_failure(message: str | Exception, status: int | None = None) -> ClaudeError:
    """
    Classify failure message into appropriate exception.

    Args:
        message: Error message from Claude CLI or Exception
        status: HTTP status code if available

    Returns:
        Appropriate ClaudeError subclass
    """
    if isinstance(message, Exception):
        message = str(message)
    msg_lower = message.lower()

    # Limit errors
    limit_patterns = [
        r"usage limit",
        r"hit your.*limit",
        r"limit reached",
        r"rate[_\s-]?limit",
        r"out of.*usage",
        r"quota",
    ]
    if status == 429 or any(re.search(p, msg_lower) for p in limit_patterns):
        epoch = _extract_epoch(message)
        reset_text = _extract_reset_text(message)
        detail = _clean_detail(message, epoch)
        return ClaudeLimitError(detail=detail, resets_at=epoch, reset_text=reset_text)

    # Auth errors
    auth_patterns = [
        r"not logged in",
        r"/login",
        r"invalid api key",
        r"authentication",
        r"unauthori[sz]ed",
        r"oauth token",
        r"invalid token",
        r"permission denied",
    ]
    if status in (401, 403) or any(re.search(p, msg_lower) for p in auth_patterns):
        detail = message[:300]
        return ClaudeAuthError(
            f"authentication failed: {message}",
            detail=detail,
        )

    # Timeout errors
    timeout_patterns = [
        r"timeout",
        r"deadline exceeded",
        r"timed out",
    ]
    if any(re.search(p, msg_lower) for p in timeout_patterns):
        return ClaudeTimeout(message[:500])

    # Generic error
    return ClaudeError(message[:500])


def classify_exception(exc: Exception) -> ClaudeError:
    """Classify an exception into appropriate ClaudeError."""
    return classify_failure(str(exc))


# Public alias for test compatibility
def redact_token(text: str) -> str:
    """Redact token from text. Public wrapper for _redact_token."""
    # Match sk-ant-oat-* and sk-ant-api* patterns
    import re
    text = re.sub(r"(sk-ant-oat-)[a-zA-Z0-9-]+", r"\1***", text)
    text = re.sub(r"(sk-ant-api\d*-)[a-zA-Z0-9-]+", r"\1***", text)
    # Handle Bearer tokens
    text = re.sub(r"Bearer\s+sk-ant-[a-zA-Z0-9-]+", "Bearer ***", text)
    return text


def parse_result(output: str) -> str:
    """Parse JSON result from Claude CLI output, extracting the 'result' field if present."""
    import json
    output = output.strip()
    if not output:
        return ""
    try:
        result = json.loads(output)
        if isinstance(result, dict):
            if "content" in result and isinstance(result["content"], list):
                texts = [block.get("text", "") for block in result["content"] if isinstance(block, dict)]
                if texts:
                    return "\n".join(texts).strip()
            if "result" in result:
                return str(result["result"])
    except json.JSONDecodeError:
        pass
    return output


ISOLATION_SETTINGS: dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {
        "cc-plugin-agents-md@builtin": False,
        "cc-plugin-telemetry@builtin": False,
        "cc-plugin-plugin-authoring@builtin": False,
    },
}


@dataclass
class InspectResult:
    """Result of inspect() call."""
    auth: str
    init: dict[str, Any]
    reply: str


class ClaudeCLI:
    """Async wrapper for Claude CLI."""

    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Path,
        model: str | None = None,
        effort: str = "low",
        timeout: int = 120,
        oauth_token: str | None = None,
        auth_label: str | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.workdir = workdir
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.oauth_token = oauth_token
        self._auth_label = auth_label or "unknown"
        self._plugins_discovered = False

    def __repr__(self) -> str:
        return f"ClaudeCLI(claude_bin={self.claude_bin!r}, model={self.model!r}, effort={self.effort!r})"

    @property
    def auth_mode(self) -> str:
        return self._auth_label

    def _build_env(self) -> dict[str, str]:
        """Build environment for child process."""
        env = os.environ.copy()

        # Remove sensitive variables
        keys_to_remove = [
            k for k in env
            if k.startswith("ANTHROPIC_")
            or k in {
                "TELEGRAM_BOT_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SIMPLE",
            }
        ]
        for k in keys_to_remove:
            env.pop(k, None)

        # Add isolation env vars
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"

        # Add OAuth token only if provided
        if self.oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.oauth_token

        return env

    def _build_command(
        self,
        system_prompt: str,
        *,
        stream_json: bool = False,
        verbose: bool = False,
    ) -> list[str]:
        """Build the claude command."""
        # Create temp file for system prompt
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.workdir,
            delete=False,
            suffix=".txt",
        ) as f:
            f.write(system_prompt)
            system_prompt_file = f.name

        try:
            os.chmod(system_prompt_file, 0o600)
        except OSError:
            pass

        cmd = [
            self.claude_bin,
            "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", "stream-json" if stream_json else "json",
            "--effort", self.effort,
        ]

        if stream_json and verbose:
            cmd.append("--verbose")

        if self.model:
            cmd.extend(["--model", self.model])

        return cmd, system_prompt_file

    async def _run_claude(
        self,
        cmd: list[str],
        stdin_data: str,
        env: dict[str, str],
        system_prompt_file: str,
    ) -> tuple[int, str, str]:
        """Run Claude CLI subprocess."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ClaudeError(f"Claude binary not found: {self.claude_bin}")

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_data.encode("utf-8")),
                timeout=self.timeout,
            )
            return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            # Kill process
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            raise ClaudeTimeout("Claude CLI timed out")
        except asyncio.CancelledError:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            raise
        finally:
            # Clean up system prompt file
            try:
                os.unlink(system_prompt_file)
            except OSError:
                pass

    def parse_result(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> str:
        """Parse Claude CLI output and return reply text."""
        # Redact token from output before parsing
        stdout_clean = _redact_token(stdout, self.oauth_token)
        stderr_clean = _redact_token(stderr, self.oauth_token)

        # Find last non-empty line of stdout
        lines = [line for line in stdout_clean.splitlines() if line.strip()]
        if not lines:
            raise classify_failure(stderr_clean or f"exit code {returncode}", returncode)

        last_line = lines[-1]
        try:
            result = json.loads(last_line)
        except json.JSONDecodeError:
            raise classify_failure(stderr_clean or stdout_clean or f"exit code {returncode}", returncode)

        # Check for error in result
        if result.get("is_error") or result.get("subtype") != "success":
            error_msg = result.get("error", {}).get("message", "") if isinstance(result.get("error"), dict) else ""
            combined = f"{error_msg} {stderr_clean}".strip()
            raise classify_failure(combined, returncode)

        # Extract text from result
        content = result.get("content", [])
        if isinstance(content, list):
            texts = [block.get("text", "") for block in content if isinstance(block, dict)]
            return "\n".join(texts).strip()
        elif isinstance(content, str):
            return content.strip()

        return ""

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        Generate a reply using Claude CLI.

        Args:
            system_prompt: System prompt with profile
            user_prompt: User prompt with conversation history

        Returns:
            Reply text (empty string if SKIP_TOKEN)
        """
        cmd, system_prompt_file = self._build_command(system_prompt)
        env = self._build_env()

        try:
            returncode, stdout, stderr = await self._run_claude(
                cmd, user_prompt, env, system_prompt_file
            )
        except (ClaudeTimeout, asyncio.CancelledError):
            raise
        except ClaudeError:
            raise
        except Exception as e:
            raise ClaudeError(f"Failed to run Claude CLI: {e}")

        try:
            reply = self.parse_result(returncode, stdout, stderr)
        except (ClaudeLimitError, ClaudeAuthError):
            raise
        except ClaudeError as e:
            # Add token redacted CLI output for auth errors
            if self.oauth_token and "auth" in str(e).lower():
                detail = _redact_token(stderr or stdout, self.oauth_token)
                raise ClaudeAuthError(
                    "Token rejected or expired. Run 'claude setup-token' in a browser machine "
                    "and update CLAUDE_CODE_OAUTH_TOKEN.",
                    detail=detail[:300],
                )
            raise

        return reply

    async def inspect(self) -> InspectResult:
        """
        Run a minimal Claude CLI call to inspect configuration.

        Returns:
            InspectResult with auth mode, init data, and reply
        """
        cmd = [
            self.claude_bin,
            "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", "-",  # Will write to temp file
            "--output-format", "stream-json",
            "--verbose",
            "--effort", self.effort,
        ]

        if self.model:
            cmd.extend(["--model", self.model])

        # Create minimal system prompt
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.workdir,
            delete=False,
            suffix=".txt",
        ) as f:
            f.write("Reply with exactly: OK")
            system_prompt_file = f.name

        try:
            os.chmod(system_prompt_file, 0o600)
        except OSError:
            pass

        # Replace "-" with actual file path
        cmd = [system_prompt_file if arg == "-" else arg for arg in cmd]

        env = self._build_env()

        try:
            returncode, stdout, stderr = await self._run_claude(
                cmd, "ping", env, system_prompt_file
            )
        except (ClaudeTimeout, asyncio.CancelledError):
            raise
        except Exception as e:
            raise ClaudeError(f"Inspect failed: {e}")

        # Parse stream-json output
        auth_mode = self._auth_label
        init_data = {}
        reply = ""

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "init":
                init_data = event.get("data", {})
            elif event.get("type") == "result":
                reply = event.get("data", {}).get("content", [{}])[0].get("text", "") if event.get("data", {}).get("content") else ""

        return InspectResult(auth=auth_mode, init=init_data, reply=reply)