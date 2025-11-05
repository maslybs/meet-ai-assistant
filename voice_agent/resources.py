from pathlib import Path


def read_instructions(path: Path) -> str:
    """
    Load the assistant instructions from `prompt.md` or an alternative path.
    Raises RuntimeError with context if the file is missing.
    """

    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Prompt file '{path}' is missing. Provide VOICE_AGENT_PROMPT_FILE or VOICE_AGENT_INSTRUCTIONS."
        ) from exc

