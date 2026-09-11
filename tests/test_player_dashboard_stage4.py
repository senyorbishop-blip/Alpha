from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_stage4_player_dashboard_tabs_summary_modes_and_empty_states():
    sheet = read("client/static/js/character/character_sheet_container.js")
    css = read("client/static/css/character-sheet-premium.css")
    play = read("client/templates/play.html")
    book = read("client/static/js/ui/character_book.js")

    for tab in ["Actions", "Spells", "Inventory", "Features", "Notes"]:
        assert f"label: '{tab}'" in sheet

    for stat in [
        "Armor Class",
        "Hit Points",
        "Temp HP",
        "Speed",
        "Initiative",
        "Proficiency",
        "Spell Save DC",
        "Spell Attack",
        "Passive Perception",
    ]:
        assert stat in sheet

    for mode in ["Build/Edit mode", "Import Review mode", "Live Play mode", "Level Up mode"]:
        assert mode in sheet or mode in play or mode in book

    assert "No spells found. This character may not cast spells, or the import needs review." in sheet
    assert "No equipped weapon found. Go to Inventory to equip one." in sheet
    assert "AC has warnings. Open Import Review to check armour/shield data." in sheet
    assert "cs-mode-strip" in css
    assert "char-book-mode-ribbon" in play
    assert "Advanced edit pages" in play
    assert "updateCharacterBookModeRibbon" in book


def test_stage4_inventory_keeps_equipment_currency_attunement_and_container_sections():
    inventory = read("client/static/js/character/tabs/inventory_tab.js")

    for section in ["Equipped", "Currency", "Attunement", "Containers", "Consumables", "Carried Items", "All Items"]:
        assert section in inventory

    assert "_renderContainers" in inventory
    assert "_renderAttunement" in inventory
