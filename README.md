# EasyClip 1.1.0

Simple clipboard history for Linux desktops (GTK3, X11). Press **Super+V**.

- Recent copies (text and images), newest first; keeps 25 by default (⚙ to change).
- Type to search. Click or press Enter to paste into the app you were in (terminals get Ctrl+Shift+V).
- ☆ Pin items you reuse; they stay at the top and survive "Clear history".
- Never saves secrets copied from password managers (KeePassXC, Bitwarden… via `x-kde-passwordManagerHint`).
- Keeps your last copy available after the app you copied from closes.
- Tray icon: left-click opens the panel, right-click lists the 8 newest items to paste, pause, clear, quit.
- ⚙: tray icon on/off, paste right away on/off, keep images, start at login, history size, pause for 5 minutes.

Run: `python3 easyclip.py` (`--hidden` to start in the background, `--show` to toggle the panel, `--quit`).
History lives in `~/.local/share/easyclip/`, settings in `~/.config/easyclip/config.json`.
Needs: python3-gi, gir1.2-gtk-3.0, xdotool, x11-utils (xprop).
