# Keyboard layouts

The phone keyboard turns each character you type into key presses on the target. A USB keyboard sends key positions, not characters, so the bridge needs to know which keys the target's layout uses for each character. That's what these files are.

The layout button in the phone keyboard's bar picks one; the choice is saved on the bridge.

| File | Layout |
| --- | --- |
| `en-US.json` | English (US) |
| `en-GB.json` | English (UK) |
| `es-ES.json` | Spanish (Spain) |
| `es-419.json` | Spanish (Latin America) |
| `pt-BR.json` | Portuguese (Brazil, ABNT2) |
| `pt-PT.json` | Portuguese (Portugal) |
| `fr-FR.json` | French (France, AZERTY) |
| `de-DE.json` | German (Germany, QWERTZ) |
| `it-IT.json` | Italian (Italy) |
| `ru-RU.json` | Russian (Russia, JCUKEN) |

Languages written through an input method on the target (Chinese, Japanese, Hindi and others) don't need their own file: you type the Latin letters, the target's input method converts them, and `en-US` covers that.

## Adding a layout

Copy the closest existing file, name it with a [language-COUNTRY code](https://en.wikipedia.org/wiki/IETF_language_tag), and edit it:

```json
{
  "name": "Spanish (Spain)",
  "keys": {
    "ñ": "Semicolon",
    "Ñ": "S+Semicolon",
    "@": "G+Digit2",
    "´": "Quote Space"
  },
  "dead": {
    "Quote": "áéíóúÁÉÍÓÚ"
  }
}
```

- `keys` maps a character to what to press for it. Each press is a [key code](https://developer.mozilla.org/en-US/docs/Web/API/UI_Events/Keyboard_event_code_values), named after where the key sits on a US keyboard: `Semicolon` is the key right of L, whatever your layout prints on it. `S+` holds Shift and `G+` holds AltGr, and several presses go one after another separated by spaces, which is how dead keys work (`Quote Space` types the accent itself).
- Letters `a` to `z` (and uppercase) and digits `0` to `9` are filled in with their US positions. List them only where your layout moves them, like `"a": "KeyQ"` on AZERTY. For a non-Latin layout, add `"latin": false` so Latin letters aren't typed on the wrong keys.
- `dead` maps a dead key to the ten vowels `aeiouAEIOU` with its accent, in that order. Each one is typed as the dead key followed by the plain vowel. Characters listed in `keys` win over these.
- Characters a layout can't type are skipped.

Check it before sending a pull request:

```bash
python3 dev/check_layouts.py
```

CI runs the same check. It catches format mistakes, not wrong keys, so please test the layout against a real target set to it.
