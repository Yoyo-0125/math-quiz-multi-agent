import re


def sanitize_math_markdown(text):
    """Repair common model/OCR LaTeX glitches before MathJax renders output."""
    if not text:
        return ""
    fixed = str(text)

    replacements = [
        (r"\\left\\\{\s*\\\s*begin\{array\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s+begin\{array\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s*\\ngarrow\\begin\{array\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s*\\nd\{array\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s*\\nglobarray\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s*ngarrow\\begin\{array\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s*nd\{array\}", r"\\left\\{\\begin{array}"),
        (r"\\left\\\{\s*nglobarray\}", r"\\left\\{\\begin{array}"),
        (r"\\\s+begin\{array\}", r"\\begin{array}"),
        (r"\{\s+begin\{array\}", r"{\\begin{array}"),
        (r"(?<!\\)begin\{array\}", r"\\begin{array}"),
        (r"(?<!\\)end\{array\}", r"\\end{array}"),
        (r"\\left\\\{\s*\\begin\{array\}\s*\{l\}", r"\\left\\{\\begin{array}{l}"),
    ]
    for pattern, replacement in replacements:
        fixed = re.sub(pattern, replacement, fixed)

    fixed = re.sub(r"(\$\$\s*)\$(\\begin\{(?:cases|array)\})", r"\1\2", fixed)
    fixed = re.sub(r"(\\end\{(?:cases|array)\})\$(\s*\$\$)", r"\1\2", fixed)
    fixed = _repair_array_segments(fixed)
    fixed = _repair_line_math_delimiters(fixed)
    return fixed


def _repair_array_segments(text):
    repaired = text
    segment_pattern = re.compile(
        r"(\\left\\\{)(?P<body>(?:(?!\\left\\\{).)*?)(\\end\{array\})",
        flags=re.DOTALL,
    )

    def repl(match):
        full = match.group(0)
        body = match.group("body")
        if r"\begin{array}" in full:
            return full
        if r"\begin{array}" in body:
            return full
        return match.group(1) + r"\begin{array}{l}" + body + match.group(3)

    repaired = segment_pattern.sub(repl, repaired)
    return repaired


def _repair_line_math_delimiters(text):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if (
            r"\begin{cases}" in stripped
            and r"\end{cases}" in stripped
            and stripped.endswith("$")
            and "$" not in stripped[:-1]
        ):
            begin_index = line.find(r"\begin{cases}")
            lines.append(line[:begin_index] + "$" + line[begin_index:])
            continue
        if (
            r"\begin{array}" in stripped
            and r"\end{array}" in stripped
            and stripped.endswith("$")
            and "$" not in stripped[:-1]
        ):
            begin_index = line.find(r"\begin{array}")
            lines.append(line[:begin_index] + "$" + line[begin_index:])
            continue
        lines.append(line)
    return "\n".join(lines)
