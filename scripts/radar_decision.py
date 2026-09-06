"""Parse a final decision without depending on Markdown decoration."""
import re


def decision(text):
    matches = []
    for line in text.splitlines():
        line = line.strip()
        labeled = re.fullmatch(r'(?:\*\*)?(?:推荐|最终结论|Recommendation|PUSH/WATCH/SKIP)(?:\*\*)?\s*[:：]\s*(?:`|\*\*)?(PUSH|WATCH|SKIP)(?:`|\*\*)?', line, re.I)
        if labeled:
            matches.append(labeled[1].upper())
            continue
        for pattern in (r'`([^`]+)`', r'\*\*([^*]+)\*\*', r'\*([^*]+)\*'):
            match = re.fullmatch(pattern, line)
            if match:
                line = match[1].strip()
                break
        if line in ('PUSH', 'WATCH', 'SKIP'):
            matches.append(line)
    return matches[-1] if matches else None
