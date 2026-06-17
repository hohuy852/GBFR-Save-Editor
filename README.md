# Granblue Fantasy Relink Save Lab

**Created by ProtoBuffers**

A clean save editor for **Granblue Fantasy Relink** with support for **PC saves** and **decrypted PS4 saves**.

> PS4 saves must be decrypted before editing. If you need help decrypting your PS4 save for free, join the ProtoBuffers Discord:  
> https://discord.gg/protobuffers

---

## Quick Overview

Granblue Fantasy Relink Save Lab is designed to make common save edits fast, readable, and safer for end users.

It includes:

- A Welcome page save finder
- Save Health checks
- Cheats dashboard
- Items / Materials editing
- Sigil editing and database add tools
- Weapon editing and add-all-missing tools
- Character editing
- Progression / unlock editing
- Mastery / Overmastery editing
- Safer value inputs with 32-bit clamps
- Save As workflow for safer testing

---

## Supported Saves

| Platform | Support |
|---|---|
| PC | Supported |
| PS4 | Supported after decryption |

The editor can search for and open saves named like:

- `GameData`
- `SaveData1`

The Welcome page can scan a selected folder and its subfolders to find supported save files.

---

## Main Features

### Welcome Save Finder

- Select a base folder
- Search through all subfolders
- Detect `GameData` and `SaveData1` saves
- Display found saves in a clean table
- Open a save by double-clicking it
- Open a selected save with one button

### Save Health

- Check current save status
- View hash/status information
- View dirty/clean edit state
- Check reusable slot information
- Review safety information before saving

### Cheats

- Max item/material quantities
- Max sigils
- Max weapons
- Max characters
- Progression/unlock shortcuts
- Mastery/Overmastery shortcuts
- Repair/helper tools where available

### Items / Materials

- View item/material rows
- Edit quantities
- Search and filter inventory rows
- Max existing quantities
- Use database add tools where safe
- Help repair certain unsafe added rows

### Sigils

- View and edit sigils
- Change sigil level
- Lock or unlock sigils
- Max sigil levels
- Add sigils from the built-in database
- View reusable empty sigil slots
- Filter unknown sigils
- Edit current sigil rows directly

The editor supports large sigil test levels up to the signed 32-bit maximum:

```text
2,147,483,647
```

### Weapons

- View and edit weapons
- Change weapon XP/progress
- Max weapon XP
- Add weapons from the built-in database
- Add all missing weapons
- View reusable empty weapon slots
- Filter known, unknown, and empty weapon rows

Weapon XP max is currently:

```text
999,999,999
```

### Characters

- View character rows
- Edit mapped character values
- Max character values
- Use cleanup/max actions for safer editing

Character values currently support:

```text
999,999,999
```

### Progression

- Main quest progress
- Side/challenge quest progress
- Fate Episode progress
- Multiplayer-related progress
- Miscellaneous unlocks

The Progression page focuses on mapped rows to avoid exposing random research/debug data to normal users.

### Mastery / Overmastery

- Pick 4 Overmastery stats
- Apply stats to one selected group
- Apply stats to all groups
- Auto-apply selected Overmastery setup
- Set Overmastery values
- Edit selected mastery rows directly

Known Overmastery value shortcuts:

| Button | Meaning |
|---|---|
| 80% | Writes raw `FFFFFFFF` |
| 20% | Writes `512` |
| Zero | Writes `0` |

Important note:

`FFFFFFFF` may display as `-1` in signed fields. That is expected and means the raw value is still `0xFFFFFFFF`.

---

## Safer Editing

The editor includes safety-focused behavior such as:

- Save As workflow
- Dirty/clean save state tracking
- Input clamps for signed 32-bit values
- Cleaner end-user navigation
- Hidden research/lookup pages in normal UI
- Reduced popups for routine actions
- Status bar messages for most normal edits

Recommended workflow:

1. Open your save.
2. Make edits.
3. Use **Save As** for the first edited copy.
4. Test the edited save in game.
5. Only overwrite originals after confirming the save works.

---

## Credits

Created by:

- **ProtoBuffers**

Community data sheet:

- https://docs.google.com/spreadsheets/d/1mGf987Njg3VodeXp8kVwzEgvYSAeMzkHnkGnAj1_RjY/edit?gid=0#gid=0

Additional credits:

- zeraf3000
- JJDarklight
- method_dev
- hywolfe
- skiller
- peepeez
- di_ciolla
- tj0816
- dvymin
- ceruleandhm
- anon devs

---

## PS4 Save Help

PS4 saves must be decrypted before they can be edited.

Need help decrypting your PS4 save for free?

Join the ProtoBuffers Discord:

https://discord.gg/protobuffers

---

## Disclaimer

Use at your own risk.

Always keep backups of your original saves before editing. Save editing can break saves if invalid values are written or if the game rejects modified data.
