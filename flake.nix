{
  description = "tt-emu — a hardware-level emulator of the Ravensburger tiptoi® pen";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # tt-emu installs its Python dependencies with pip/uv into a venv.
        # Several are C-extension / CFFI wheels that resolve their native
        # libraries at *run time* via dlopen — and NixOS has no global
        # /usr/lib — so those libraries must be on LD_LIBRARY_PATH. The one
        # that actually bites is libportaudio (sounddevice, for live audio);
        # libstdc++ / libz cover any wheel that wants them.
        runtimeLibs = with pkgs; [
          portaudio         # sounddevice -> libportaudio.so.2 (live audio output)
          stdenv.cc.cc.lib  # libstdc++.so.6 / libgcc_s
          zlib
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python3           # create the venv: pip/uv install -e .
            uv                # uv venv / uv pip, if you prefer it to pip
            ruff              # linter (the nixpkgs build runs on NixOS; the wheel doesn't)
            gcc-arm-embedded  # arm-none-eabi-gcc/objcopy: the test-firmware toolchain
            gnumake           # the tests/firmware Makefile
          ];

          # sounddevice/etc. dlopen their C libraries at runtime; put them on
          # the loader path (see runtimeLibs above).
          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
            echo "tt-emu devShell: arm-none-eabi-gcc + PortAudio on the library path."
            echo "  first time:  python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'"
            echo "  run:         tt-emu --gme path/to/game.gme"
          '';
        };
      });
}
