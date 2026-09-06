"""Parse a final decision without depending on Markdown decoration."""
import re


def decision(text):
    matches = []
    for line in text.splitlines():
        line = line.strip()
        for pattern in (r'`([^`]+)`', r'\*\*([^*]+)\*\*', r'\*([^*]+)\*'):
            match = re.fullmatch(pattern, line)
            if match:
                line = match[1].strip()
                break
        if line in ('PUSH', 'WATCH', 'SKIP'):
            matches.append(line)
    return matches[-1] if matches else None
