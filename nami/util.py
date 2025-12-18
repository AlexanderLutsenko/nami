from typing import List


def build_exclude_flags_s3(patterns: str) -> str:
    """Return aws_exclude_flags for given comma patterns."""
    parts: List[str] = []
    for p in (s.strip() for s in patterns.split(",")):
        if p:
            parts.append(f'--exclude "{p}"')
    return " ".join(parts)


def build_include_flags_s3(patterns: str) -> str:
    """Return aws_include_flags for given comma patterns."""
    parts: List[str] = []
    for p in (s.strip() for s in patterns.split(",")):
        if p:
            parts.append(f'--include "{p}"')
    return " ".join(parts)


def build_exclude_flags_zip(patterns: str) -> str:
    """Return zip_exclude_flags for given comma patterns."""
    parts: List[str] = []
    for p in (s.strip() for s in patterns.split(",")):
        if p:
            parts.append(f'-x "{p}"')
    return " ".join(parts) 