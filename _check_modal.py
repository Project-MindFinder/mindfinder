"""임시 검증: 모달에 Select + Label(FileUpload) 를 함께 넣을 수 있는지."""
import discord

print("discord.py", discord.__version__)

m = discord.ui.Modal(title="t")
try:
    s = discord.ui.Select(
        custom_id="mf:hospital:select",
        placeholder="병원 선택",
        options=[discord.SelectOption(label="서울대학교병원 (62병동)", value="1")],
    )
    m.add_item(s)
    print("OK: raw Select added to modal")
except Exception as exc:  # noqa: BLE001
    print("FAIL Select:", type(exc).__name__, exc)

try:
    f = discord.ui.FileUpload(custom_id="mf:truth_pdf", required=False, max_values=1)
    m.add_item(discord.ui.Label(text="서류 PDF", component=f))
    print("OK: Label(FileUpload) added to modal")
except Exception as exc:  # noqa: BLE001
    print("FAIL FileUpload:", type(exc).__name__, exc)

try:
    import json

    data = m.to_dict()
    print("SERIALIZED_OK")
    print(json.dumps(data, ensure_ascii=False)[:1500])
except Exception as exc:  # noqa: BLE001
    print("FAIL serialize:", type(exc).__name__, exc)
