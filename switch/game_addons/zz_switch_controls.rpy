# Nintendo Switch controls for Ren'Py games (RenpySwitch).
#
# Drop this file into the game's "game" folder. It only does anything on the
# Switch build (renpy.switch); on PC/Android it is ignored.
#
# Joy-Con / Pro Controller (Nintendo button labels):
#   A            next line / select
#   B            back / open the game menu
#   X, Minus     hide the text box (look at the picture)
#   Y            history (backlog)
#   ZR           skip on/off
#   ZL           auto mode on/off
#   L            quick save
#   R            quick load
#   Plus         game menu
#   Right stick  up = previous line (rollback), down = next line (rollforward)
#   Left stick / D-pad   move between buttons and menu choices
#
# Touch screen (handheld):
#   tap = next line / press a button, long press = game menu,
#   swipe up = history, swipe down = hide the text box.
#
# Button names below are SDL's positional (Xbox-style) names. With the
# devkitPro SDL2 mapping the physical Nintendo buttons are:
#   A -> "b", B -> "a", X -> "y", Y -> "x", L/R -> shoulders, ZL/ZR -> triggers,
#   Plus -> "start", Minus -> "back".

init 1000 python:

    if getattr(renpy, "switch", False):

        def _switch_bind(control, *events):
            config.pad_bindings[control] = list(events)

        # Face buttons.
        _switch_bind("pad_b_press", "dismiss", "button_select", "bar_activate", "bar_deactivate")  # A
        _switch_bind("pad_a_press", "game_menu")                                                   # B
        _switch_bind("pad_y_press", "hide_windows")                                                # X
        _switch_bind("pad_x_press", "switch_history")                                              # Y

        # Plus / Minus.
        _switch_bind("pad_start_press", "game_menu")
        _switch_bind("pad_guide_press", "game_menu")
        _switch_bind("pad_back_press", "hide_windows")
        config.pad_bindings.pop("repeat_pad_back_press", None)

        # Triggers: ZR skip toggle, ZL auto-forward toggle.
        _switch_bind("pad_righttrigger_pos", "switch_skip")
        _switch_bind("pad_lefttrigger_pos", "switch_auto")
        config.pad_bindings.pop("repeat_pad_lefttrigger_pos", None)

        # Shoulders: quick save / quick load.
        _switch_bind("pad_leftshoulder_press", "switch_quicksave")
        _switch_bind("pad_rightshoulder_press", "switch_quickload")
        config.pad_bindings.pop("repeat_pad_leftshoulder_press", None)
        config.pad_bindings.pop("repeat_pad_rightshoulder_press", None)

        # Right stick vertical: rollback / rollforward (like a mouse wheel).
        # Horizontal stays on focus movement.
        for _prefix in ("", "repeat_"):
            config.pad_bindings[_prefix + "pad_righty_neg"] = ["rollback"]
            config.pad_bindings[_prefix + "pad_righty_pos"] = ["rollforward"]

        # Touch gestures (only active in handheld mode, "touch" variant).
        config.gestures.update({
            "n": "switch_history",
            "s": "hide_windows",
        })

        # Actions for the custom event names used above. The underlay is
        # active everywhere, so these work in game and in menus.
        config.underlay.append(
            renpy.Keymap(
                switch_history=ShowMenu("history") if renpy.has_screen("history") else NullAction(),
                switch_skip=Skip(),
                switch_auto=Preference("auto-forward", "toggle"),
                switch_quicksave=QuickSave(),
                switch_quickload=QuickLoad(confirm=False),
            )
        )

        # Register the custom event names (no keyboard keys bound to them).
        config.keymap.setdefault("switch_history", [])
        config.keymap.setdefault("switch_skip", [])
        config.keymap.setdefault("switch_auto", [])
        config.keymap.setdefault("switch_quicksave", [])
        config.keymap.setdefault("switch_quickload", [])
