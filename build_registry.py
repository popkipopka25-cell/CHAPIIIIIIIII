"""Строит реестр команд Iris из сохранённого текста каталога (docs/iris_catalog.txt)
и сопоставляет с реализованными командами Чапи (IMPLEMENTED ниже)."""
import re, csv, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "iris_catalog.txt"

# Команды Чапи, реально подключённые к боту (по регуляркам в handlers/)
IMPLEMENTED = {
    r"^\+?модер|^повысить|^понизить|^роль": ("частично", "роль @user N (ранги 0-5)"),
    r"^кто админ": ("да", "кто админ"),
    r"^мой id": ("да", "мой id"),
    r"^инфо группы": ("да", "инфо группы"),
    r"^проверить мут": ("да", "проверить мут @user"),
    r"^муты": ("да", "муты"),
    r"^варнлист": ("да", "варнлист"),
    r"^мои варны": ("да", "мои варны"),
    r"^снять все варны": ("да", "снять все варны @user"),
    r"^банлист": ("да", "банлист"),
    r"^причина": ("да", "причина @user"),
    r"^мут": ("да", "мут @user 10м причина"),
    r"^размут|^говори": ("да", "размут @user"),
    r"^варн\b|^пред\b": ("да", "варн @user причина"),
    r"^варны|^преды": ("да", "варны @user"),
    r"^снять варн|^-варн|^разварн": ("да", "снять варн @user"),
    r"^бан\b": ("да", "бан @user"),
    r"^разбан": ("да", "разбан @user"),
    r"^кик\b": ("да", "кик @user"),
    r"^профиль|^анкета|^кто я|^кто ты": ("частично", "профиль"),
    r"^баланс|^мешок": ("частично", "баланс"),
    r"^бонус": ("да", "бонус"),
    r"^история|^транзакции": ("да", "история"),
    r"^передать (ирис|\{число)|^перевести": ("частично", "перевести @user N"),
    r"^топ": ("частично", "топ"),
    r"^\+?заметка|^заметки|^-заметка": ("да", "заметка создать/удалить, заметки"),
    r"^помощь|^команды": ("да", "помощь"),
    r"^лог\b|^журнал": ("частично", "журнал"),
}

def parse():
    lines = SRC.read_text(encoding="utf-8").splitlines()
    section, sub, items, cur = None, None, [], None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\d{1,2})\. (.+)$", ln)
        if m and i > 125:
            section = f"{m[1]}. {m[2]}"; sub = None; cur = None; continue
        if section is None:
            continue
        if ln == "・" or ln.startswith("・"):
            cur = {"cat": section, "sub": sub, "text": ln.lstrip("・").strip()}
            items.append(cur); continue
        if cur is not None and (ln.startswith("—") or ln.startswith("–") or len(cur["text"]) < 400) and not ln.startswith("🎩"):
            if re.match(r"^[А-ЯЁA-Z][^—]{3,60}$", ln) and not ln.startswith("Синоним") and cur["text"].count("—") >= 1 and not cur["text"].endswith("{"):
                # возможный подзаголовок
                sub = ln; cur = None; continue
            cur["text"] += " " + ln
        elif re.match(r"^[А-ЯЁA-Z][^—]{3,60}$", ln):
            sub = ln
    out = []
    for it in items:
        t = re.sub(r"\s+", " ", it["text"]).strip()
        syn = ""
        ms = re.search(r"Синоним[ы]?:?\s*(.+)$", t)
        if ms:
            syn = ms[1].strip(" .;"); t = t[:ms.start()].rstrip(" —–")
        parts = re.split(r"\s[—–]\s", t, maxsplit=1)
        cmd = parts[0].strip(); desc = parts[1].strip() if len(parts) > 1 else ""
        rights = "модератор" if it["cat"].split(".")[0] in {"1","2","3","4","5","6","7","10"} else "участник"
        status, chapi = "нет", ""
        low = cmd.lower().lstrip("!./+ ")
        for rx, (st, c) in IMPLEMENTED.items():
            if re.search(rx, low) or re.search(rx, cmd.lower()):
                status, chapi = st, c; break
        out.append(dict(category=it["cat"], subsection=it["sub"] or "", command=cmd, synonyms=syn,
                        purpose=desc[:300], rights=rights, chapi_status=status, chapi_command=chapi,
                        todo="" if status == "да" else "реализовать аналог/дополнить",
                        tests="юнит-тест сервиса + проверка прав"))
    return out

if __name__ == "__main__":
    rows = parse()
    with open(ROOT / "docs" / "commands_registry.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    md = ["# Реестр команд Iris → Чапи", "", f"Источник: https://teletype.in/@iris_cm/commands (снимок: docs/iris_catalog.txt). Всего пунктов: {len(rows)}.", ""]
    cat = None
    for r in rows:
        if r["category"] != cat:
            cat = r["category"]; md += ["", f"## {cat}", "", "| Команда | Синонимы | Назначение | Права | Чапи | Команда Чапи |", "|---|---|---|---|---|---|"]
        esc = lambda s: s.replace("|", "/")
        md.append(f"| {esc(r['command'])} | {esc(r['synonyms'])} | {esc(r['purpose'][:160])} | {r['rights']} | {r['chapi_status']} | {esc(r['chapi_command'])} |")
    (ROOT / "docs" / "COMMANDS_REGISTRY.md").write_text("\n".join(md), encoding="utf-8")
    from collections import Counter
    print(len(rows), Counter(r["chapi_status"] for r in rows))
    print(Counter(r["category"] for r in rows))
