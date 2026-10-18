"""Build tag_translations.py from the EhTagTranslation database.

Source: https://github.com/EhTagTranslation/Database (CC BY-NC-SA 3.0), which translates EHWiki's
tag documentation. That project is the community's answer to exactly this problem, maintained with
per-entry descriptions and icons; re-typing tags by hand was always going to lose to it.

**Two transforms happen here, and both matter.**

1. **Simplified to Traditional**, with `s2twp` rather than `s2t`. `s2t` maps characters only, so
   "打印机" would stay 打印機 instead of becoming 印表機 - character conversion is not a translation.
   `s2twp` also converts vocabulary, which is what Taiwan actually reads.

2. **The icon markup is stripped.** A name cell can read `![贝合图标](https://...)贝合`, so a plain
   split keeps an img tag in the middle of what becomes a hashtag.

**artist and group are deliberately excluded.** They are 30,000 entries between them, and their
"translations" are romanisation of a pen name - `endou hiroto` becomes 遠藤弘土, which nobody searches
by and which is longer than what it replaces. The remaining ten namespaces are the ones a reader
actually benefits from having in Chinese.

Where the database and the hand-written table disagree, **the database wins** - it is reviewed by a
community against EHWiki, and several of the hand-written entries turned out to be wrong rather than
merely different (`touhou` is `touhou project` on e-hentai, so that key could never have matched).
Hand-written entries the database does not have are kept, because they were added for a reason.
"""
from pathlib import Path
import re

DATA = Path(__file__).resolve().parent.parent.parent / "_ehtags"
PROJECT = Path(__file__).resolve().parent.parent / "bot" / "tag_translations.py"

# Included; see the docstring for why artist and group are not.
# **Ordered so the last one wins, and that is not alphabetical.**
#
# The same raw name appears in several namespaces with different meanings: `angel` is 天使 as a female
# tag and 安吉爾 as a character name, `alic` is the fairytale one and Alice Margatroid the other. The
# bot looks a tag up by value alone, so only one can survive - and it should be the descriptive tag,
# which appears on galleries of every kind, not a character name that happens to share the spelling.
#
# So the most specific (character) is parsed first and the most general (female) last.
NAMESPACES = ["character", "cosplayer", "parody", "reclass", "location",
              "other", "language", "mixed", "male", "female"]

ICON = re.compile(r"!\[[^\]]*\]\([^)]*\)")
SECTION = re.compile(r"^\|\s*\|\s*===")
# The header row is `| 原始标签 | 名称 | 描述 | 外部链接 |`, in Chinese rather than English, which is
# how it slipped past a filter that only knew the word "raw".
HEADER_WORDS = {"原始标签", "原始標籤", "raw", "name", "名称", "名稱"}


def parse(stem: str) -> dict:
    path = DATA / f"{stem}.md"
    out, skipped = {}, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or SECTION.match(line):
            continue
        body = line.strip().strip("|")
        # An escaped pipe inside a cell is part of the text: `URAN \| 雨兰` is one artist, not two
        # columns. Replacing it before the split keeps the columns aligned.
        body = body.replace("\\|", "\u0000")
        parts = [c.strip().replace("\u0000", "|") for c in body.split("|")]
        if len(parts) < 2:
            continue
        raw, name = parts[0], parts[1]
        if not raw or raw in HEADER_WORDS or set(raw) <= {"-", ":", " "}:
            skipped += 1
            continue
        name = ICON.sub("", name).strip()
        # Some upstream names carry a decoration - `外星人👽` - which makes a hashtag ugly and adds
        # nothing. Only emoji and symbols are removed; parentheses and dots are part of real names.
        name = "".join(
            ch for ch in name
            if not (0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF)
        ).strip()
        # A name that is still markup, or empty, is not a translation.
        if not name or name.startswith("!["):
            skipped += 1
            continue
        out[raw.lower()] = name
    return out


def main() -> None:
    from opencc import OpenCC

    to_tw = OpenCC("s2twp")

    database = {}
    for stem in NAMESPACES:
        parsed = parse(stem)
        database.update(parsed)
        print(f"  {stem:12} {len(parsed):6} 筆")

    print(f"  資料庫合計 {len(database)} 筆")

    # Read the hand-written extras from their own module, **not from the generated file**.
    #
    # Reading the generated file back made this non-idempotent: the second run treated the first
    # run's output as hand-written input, so the "preserved" count grew every time and the file
    # could never be regenerated to the same content. A generated file that cannot be regenerated
    # reproducibly is worse than a hand-maintained one.
    import ast
    extra_path = PROJECT.parent / "tag_translations_extra.py"
    handwritten = {}
    if extra_path.exists():
        tree = ast.parse(extra_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                if getattr(node.targets[0], "id", "") == "TAG_TRANSLATIONS_EXTRA":
                    handwritten = ast.literal_eval(node.value)
                    break
    print(f"  手寫補充 {len(handwritten)} 筆")

    # Convert, then merge - the database over the hand-written table.
    converted = {key: to_tw.convert(value) for key, value in database.items()}
    merged = dict(handwritten)
    overwritten = {k: (merged[k], converted[k]) for k in converted
                   if k in merged and merged[k] != converted[k]}
    merged.update(converted)

    print(f"  合併後 {len(merged)} 筆")
    print(f"  資料庫覆蓋手寫的 {len(overwritten)} 筆；樣本：")
    for key in sorted(overwritten)[:8]:
        before, after = overwritten[key]
        print(f"    {key:24} {before} -> {after}")

    # Only keys the hand-written table has and the database does not.
    kept = sorted(k for k in handwritten if k not in converted)
    print(f"  保留手寫（資料庫沒有）{len(kept)} 筆：{kept[:10]}")

    header = '''"""
e-hentai tag translations: English -> Traditional Chinese (Taiwan).

**The data comes from [EhTagTranslation/Database](https://github.com/EhTagTranslation/Database)**,
which translates EHWiki's tag documentation. It is licensed CC BY-NC-SA 3.0; this file is a derived
work and carries the same licence. See CREDITS.md.

Regenerate with `tools/build_tag_translations.py` rather than editing the dict by hand. Two things
that script does and a text editor does not:

  - converts Simplified to Traditional with OpenCC's ``s2twp``, which handles vocabulary as well as
    characters - ``打印机`` becomes 印表機 rather than 打印機
  - strips the inline icon markup the upstream name column carries, which would otherwise end up
    inside a hashtag

``artist`` and ``group`` are excluded upstream of this file: 30,000 entries whose "translation" is the
Chinese spelling of a pen name, which is longer than the Roman one and not what anyone searches by.

Unknown tags are returned unchanged rather than dropped. e-hentai invents names per work, so a
character missing from this table is expected, not a fault.
"""

TAG_TRANSLATIONS = {
'''

    lines = [header]
    for key in sorted(merged):
        lines.append(f"    {key!r}: {merged[key]!r},\n")
    lines.append("}\n")
    lines.append('''

def translate_tag(value: str) -> str:
    """
    Return the Traditional Chinese name for a tag value.

    Unknown tags are returned unchanged rather than dropped: a reader is better served by
    the original English than by a missing tag.
    """
    if not value:
        return value
    return TAG_TRANSLATIONS.get(value.strip().lower(), value)
''')

    PROJECT.write_text("".join(lines), encoding="utf-8")
    size = PROJECT.stat().st_size
    print()
    print(f"  寫入 {PROJECT}")
    print(f"  大小 {size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
