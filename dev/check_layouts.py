#!/usr/bin/env python3
"""Checks every web/layouts/*.json against the format in web/layouts/README.md."""

import json
import pathlib
import re
import sys

LAYOUTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "layouts"
NAME_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,4})?$")
STROKE_RE = re.compile(r"^(S\+)?(G\+)?([A-Za-z0-9]+)$")
CODES = (
    {f"Key{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
    | {f"Digit{d}" for d in range(10)}
    | {"Backquote", "Minus", "Equal", "BracketLeft", "BracketRight", "Backslash", "Semicolon", "Quote",
       "Comma", "Period", "Slash", "IntlBackslash", "IntlRo", "IntlYen", "Space", "Enter", "Tab"}
)


def check_strokes(strokes):
    for stroke in strokes.split(" "):
        match = STROKE_RE.match(stroke)
        if not match or match.group(3) not in CODES:
            return f"bad stroke {stroke!r}"
    return None


def check(path):
    errors = []
    if not NAME_RE.match(path.stem):
        errors.append("file name must be a language-COUNTRY code like es-ES")
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"invalid JSON: {exc}"]
    if not isinstance(layout.get("name"), str):
        errors.append('"name" must be a string')
    if not isinstance(layout.get("latin", True), bool):
        errors.append('"latin" must be true or false')
    keys = layout.get("keys")
    if not isinstance(keys, dict):
        return errors + ['"keys" must be an object']
    for char, strokes in keys.items():
        if len(char) != 1:
            errors.append(f"key {char!r} must be a single character")
        elif not isinstance(strokes, str) or check_strokes(strokes):
            errors.append(f"{char!r}: {check_strokes(strokes) if isinstance(strokes, str) else 'must be a string'}")
    dead = layout.get("dead", {})
    if not isinstance(dead, dict):
        return errors + ['"dead" must be an object']
    for strokes, accented in dead.items():
        if check_strokes(strokes):
            errors.append(f"dead key {strokes!r}: {check_strokes(strokes)}")
        if not isinstance(accented, str) or len(accented) != 10:
            errors.append(f"dead key {strokes!r}: needs the 10 accented vowels aeiouAEIOU")
    base = layout.get("extends")
    if base is not None and not (LAYOUTS_DIR / f"{base}.json").is_file():
        errors.append(f'"extends": no layout {base!r}')
    unknown = set(layout) - {"name", "extends", "latin", "keys", "dead"}
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")
    return errors


def main():
    paths = sorted(LAYOUTS_DIR.glob("*.json"))
    failed = False
    for path in paths:
        errors = check(path)
        for error in errors:
            print(f"{path.name}: {error}")
        failed = failed or bool(errors)
    for path in paths:
        seen = [path.stem]
        while True:
            try:
                base = json.loads((LAYOUTS_DIR / f"{seen[-1]}.json").read_text(encoding="utf-8")).get("extends")
            except (OSError, ValueError, AttributeError):
                break
            if base is None:
                break
            if base in seen:
                print(f"{path.name}: extends loop {' -> '.join(seen + [base])}")
                failed = True
                break
            seen.append(base)
    if "en-US.json" not in {p.name for p in paths}:
        print("en-US.json is missing; it's the fallback")
        failed = True
    print(f"{len(paths)} layouts checked" + (", with errors" if failed else ", all fine"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
