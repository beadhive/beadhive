{
  # The LOCAL-INSTALL toolchain (bh-q160.12). NOT the developer toolchain — macOS development
  # stays on mise + Brewfile, and `just bootstrap` is unchanged. This flake exists for the one
  # job mise did badly: making every dependency reachable BY `bh` on a Linux host nobody is
  # sitting at.
  #
  # WHY: `bh` resolves tools with shutil.which() on the inherited PATH (deps.py :: DEPS, via
  # setup.probe_one).
  # mise installs into its own tree and only reaches PATH once activated, so tools were
  # structurally invisible — measured on a bare Debian 13 host, `bh setup check` found 2 of 4
  # after a SUCCESSFUL `just bootstrap`. Under this flake it finds 4 of 4. A Nix store path is a
  # real binary on PATH; there is no install-vs-activate gap to fall into.
  #
  # Nix itself is installed BY ROOT in multi-user/daemon mode during phase-1 provisioning, in
  # the same root phase that creates bees:8335 and installs OS packages. Everything below then
  # runs unprivileged.
  description = "beadhive local-install toolchain";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      # aarch64-darwin is SUPPORTED for local-install as of 2026-08-06 (bh-vmdq.1, amending
      # ADR Decision 5 / bh-q160.12, which previously scoped macOS out). macOS DEVELOPMENT
      # still stays on mise + Brewfile — Decision 1 is untouched; only the local-install plane
      # moved.
      #
      # x86_64-darwin is ABSENT deliberately: `nix eval` for it fails with "Nixpkgs 26.11 has
      # dropped support for x86_64-darwin" — Intel Macs are gone from nixpkgs-unstable, so
      # listing it would only ever produce an evaluation error. The managed path on macOS is
      # therefore Apple Silicon ONLY.
      #
      # PROVEN STATE, do not assume beyond it: x86_64-linux (beadhive-factory, 2026-08-05 —
      # `bh setup check` 4/4) and aarch64-darwin (macOS 14.5 / Apple Silicon, 2026-08-06 —
      # `nix build .#default` cold in 130s: 155 paths substituted, ONE source build,
      # `beadsRelease`; zero Rust builds, `git-workspace` came from the cache). aarch64-linux
      # EVALUATES and nothing more — in scope for local-install and untested only because no
      # arm64 Linux host was available; treat a first run there as unproven.
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAll = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };

      # Package the immutable upstream release archives, rather than rebuilding either CLI from
      # source. The version, release commit and GitHub-published archive digests are kept together
      # here so the binary that is qualified is exactly the binary installed. Beads v1.3.0 was
      # cut at f45b249ce6b40ba62aecc03949e6371e8f7c79d8 and supports this hive's v62 -> v66
      # migration; that migration remains a later, explicitly gated operation.
      beadsReleaseCommit = "f45b249ce6b40ba62aecc03949e6371e8f7c79d8";
      beadsReleaseAssets = {
        x86_64-linux = {
          asset = "beads_1.3.0_linux_amd64.tar.gz";
          hash = "sha256-L5K5BOzzW2B+RNxcOSKRc69pxU8Rg+jXCfF3NUDNzzs=";
        };
        aarch64-linux = {
          asset = "beads_1.3.0_linux_arm64.tar.gz";
          hash = "sha256-TOlEamjtwTICt2yEpH1m+z2tJ4e2RkyVIqEI+vnBxgg=";
        };
        aarch64-darwin = {
          asset = "beads_1.3.0_darwin_arm64.tar.gz";
          hash = "sha256-fMdzZ9C4TFAkOhEIvB9zZIaZIRJX1BS5F1QL+Gjmu4U=";
        };
      };
      beadsRelease = pkgs:
        let release = beadsReleaseAssets.${pkgs.stdenv.hostPlatform.system}; in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "beads";
          version = "1.3.0";
          src = pkgs.fetchurl {
            url = "https://github.com/gastownhall/beads/releases/download/v1.3.0/${release.asset}";
            inherit (release) hash;
          };
          sourceRoot = ".";
          installPhase = ''
            runHook preInstall
            install -Dm755 bd "$out/bin/bd"
            install -Dm644 LICENSE "$out/share/licenses/beads/LICENSE"
            install -Dm644 README.md CHANGELOG.md -t "$out/share/doc/beads"
            runHook postInstall
          '';
          passthru.releaseCommit = beadsReleaseCommit;
          meta = {
            description = "Beads issue tracker for coding agents";
            homepage = "https://github.com/gastownhall/beads";
            license = pkgs.lib.licenses.mit;
            mainProgram = "bd";
            platforms = builtins.attrNames beadsReleaseAssets;
          };
        };

      # In this hive's shared-server mode the standalone Dolt CLI IS the server, so its version
      # governs every database on the server. Dolt 2.3.5 was functionally smoke-tested in
      # bh-28emy.5 from this same official archive; installing it does not authorize opening or
      # migrating a production hive. The release target is ad65af6cc937d10fa3c88e2041fed4325968b581.
      doltReleaseCommit = "ad65af6cc937d10fa3c88e2041fed4325968b581";
      doltReleaseAssets = {
        x86_64-linux = {
          asset = "dolt-linux-amd64.tar.gz";
          hash = "sha256-xJ1MPgBM8VgboNSgDFAjom+E6y7BXV/odu7TbVND9GM=";
        };
        aarch64-linux = {
          asset = "dolt-linux-arm64.tar.gz";
          hash = "sha256-nOcPyB5QE56XdY739NxX6Vg+Tl7wWtdddTXDDKoWE4c=";
        };
        aarch64-darwin = {
          asset = "dolt-darwin-arm64.tar.gz";
          hash = "sha256-rR43cKzLt+igWQaSKOrSK99vJ1kTEhJFG0T1GGYbjkA=";
        };
      };
      doltRelease = pkgs:
        let release = doltReleaseAssets.${pkgs.stdenv.hostPlatform.system}; in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "dolt";
          version = "2.3.5";
          src = pkgs.fetchurl {
            url = "https://github.com/dolthub/dolt/releases/download/v2.3.5/${release.asset}";
            inherit (release) hash;
          };
          sourceRoot = builtins.replaceStrings [ ".tar.gz" ] [ "" ] release.asset;
          installPhase = ''
            runHook preInstall
            mkdir -p "$out"
            cp -R . "$out/"
            runHook postInstall
          '';
          passthru.releaseCommit = doltReleaseCommit;
          meta = {
            description = "Dolt version-controlled SQL database";
            homepage = "https://github.com/dolthub/dolt";
            license = pkgs.lib.licenses.asl20;
            mainProgram = "dolt";
            platforms = builtins.attrNames doltReleaseAssets;
          };
        };

      # Exactly what `bh` requires unconditionally, plus what it shells out to at runtime.
      # The source of truth is `src/beadhive/deps.py` — every row with `required == "always"`;
      # `setup.PROBE_TABLE` is that same derivation, not a second list (bh-hsus.3).
      #
      # DELIBERATELY HAND-MIRRORED, with a test instead of codegen (bh-hsus.2 Q4). Deriving
      # this list works two ways — `builtins.fromJSON (builtins.readFile ./deps.json)` under
      # pure eval, or import-from-derivation — but both trade a hand-mirrored flake for a
      # hand-mirrored generated file plus a codegen step, and the name -> attribute map below
      # stays manual regardless (bd is a release-pinned override, not `pkgs.bd`; git, uv and just are not
      # deps.py rows at all). `tests/test_flake_toolchain.py` is the drift gate these comments
      # were only pretending to be.
      #
      # `just` is here because the ENTRY POINT is a just recipe: install.sh (bh-q160.6) runs
      # `nix develop --command just local-install`, and without it that command dies with
      # `exec: just: not found` — measured on beadhive-factory, 2026-08-05, which is the same
      # just-circularity the mise plane hit and solved with `mise exec --`. It is not a bh
      # runtime dependency and so is deliberately not a deps.py row; it is a dependency of the
      # local-install PATH, exactly like git and uv.
      #
      # A container runtime is NOT here: it is an operator-supplied prerequisite (bh-q160.1),
      # and native mode needs none at all. Neither is an agent harness: nixpkgs carries both
      # (claude-code 2.1.220, codex 0.146.0 as of the pinned rev), but claude-code is UNFREE
      # and adding it would make every `nix develop` here fail without `allowUnfree` — which
      # is the same "you accept those terms yourself" line `harness.py` already draws.
      toolchainFor = pkgs: [
        (beadsRelease pkgs)   # bd — deps.py, required always
        (doltRelease pkgs)    #      deps.py, required always
        pkgs.gh               #      deps.py, required always
        pkgs.git-workspace    #      deps.py, required always. 1.10.1 prebuilt — the mise/brew
                              #      routes both needed a Rust toolchain plus apt libssl-dev
                              #      + pkg-config.
        pkgs.procps           #      deps.py, required always (bh-x2yy0). `ps` — the orphan-seat
                              #      reap and ADR 0004's pid_start liveness probe. Undeclared
                              #      until `bh work loop` died on a host without it, as a bare
                              #      ExceptionGroup naming nothing.
        pkgs.git
        pkgs.uv               # installs bh itself
        pkgs.just             # runs `local-install`, the entry point itself (bh-q160.5)
      ];
      # THE IMAGE'S SET (bh-8b8o.1), DERIVED from the list above rather than hand-written a second
      # time. The point of nixifying docker/Dockerfile was to stop maintaining one toolchain in two
      # places; a parallel list here would reintroduce exactly that drift one layer down. Two
      # deltas, each with a reason:
      #
      #   -procps   GPL-2.0+ / LGPL-2.1+, and the same story as -git below in every respect
      #             (bh-x2yy0): the base image's apt layer already provides `ps`, and naming it
      #             here would move it into "a component we pin" where copyleft is not in
      #             ALLOWED. It IS a host requirement, hence its row in `toolchainFor` — the
      #             image satisfies that requirement from apt rather than from nix.
      #
      #   -git      GPL-2.0. It reaches the image from the base image's apt, where
      #             tests/test_component_licenses.py scopes it out as "separate programs invoked as
      #             programs", alongside Debian's hundreds of other GPL/LGPL packages. NAMING it
      #             here moves it into "a component we pin", where copyleft is not in ALLOWED and
      #             the gate correctly rejects it. What the transitive CLOSURE drags in is a
      #             different question — answered in docs/ASSURANCE.md (bh-8b8o.2): closure
      #             dependencies are base-layer, only what we NAME is a pinned component.
      #
      #   -uv       already in the image, copied by INDEX DIGEST from the official distroless uv
      #             image — a stronger pin than a nixpkgs version, and one docker/write-manifest.sh
      #             already reports. Taking it from here too would put two uv binaries on PATH with
      #             precedence decided by ordering: the same second-copy-shadows-the-first bug
      #             harness.py hit with npm-beside-native (bh-hsus.1). Caught by listing `bin/` of
      #             the built closure, which had `uv` and `uvx` in it.
      #
      #   +jq       the image's own scripts need them; docker/write-manifest.sh is jq all the way
      #   +yq-go    down. They are NOT deps.py rows and do not belong in `toolchainFor`, which
      #             states what `bh` requires on a HOST — putting them there to save three lines
      #             would make that list mean two things at once.
      #
      # `yq-go` is mikefarah's Go yq, which this repo's scripts are written against. nixpkgs' `yq`
      # is the unrelated Python jq-wrapper with different syntax; picking it would fail at runtime
      # rather than here, which is the worst place for this particular mistake to surface.
      imageToolchainFor = pkgs:
        builtins.filter (p: p != pkgs.git && p != pkgs.uv && p != pkgs.procps) (toolchainFor pkgs)
        ++ [ pkgs.jq pkgs.yq-go ];

      # WHAT EACH SHIPPED BINARY IS AND WHAT IT IS LICENSED UNDER (bh-8b8o.2), straight from
      # nixpkgs rather than from a comment block someone has to remember to update. Two consumers,
      # one export: docker/write-manifest.sh (which otherwise parses seven different `--version`
      # formats) and tests/test_component_licenses.py (which otherwise trusts hand-written rows).
      #
      # ALWAYS A LIST. nixpkgs' `meta.license` is a single attrset for most packages and a LIST for
      # multi-licensed ones, and a consumer written against only the first shape silently reads
      # `null` for the second. Normalising here means the gate has one shape to check and can
      # require EVERY id to be allowed, rather than whichever one happened to be first.
      #
      # MISSING BECOMES "UNKNOWN", NOT "". nixpkgs metadata is not a legal audit — it is
      # occasionally absent and occasionally wrong. An empty string reads as "no restriction" to
      # anything scanning this file; UNKNOWN is a value the gate can refuse, and does.
      spdxOf = p:
        let
          l = p.meta.license or null;
          ids =
            if l == null then [ ]
            else if builtins.isList l then map (x: x.spdxId or "UNKNOWN") l
            else [ (l.spdxId or "UNKNOWN") ];
        in
        if ids == [ ] then [ "UNKNOWN" ] else ids;

      # PACKAGE NAME != BINARY NAME for two of these, and both consumers need the binary name:
      # `bh setup check` matches manifest rows against `deps.py`, which says `bd`, and the image's
      # PATH carries `yq`. Emitting only the nixpkgs pname would rename two components in the
      # manifest and quietly break that lookup. Both names are kept — `name` is what the tool is
      # called, `package` is where it came from — so neither consumer has to guess and the source
      # field can still point at the real attribute.
      #
      # A two-entry hand map, deliberately, and it lives here beside the package list rather than
      # in a consumer: nothing derives a binary name from a derivation, and splitting the knowledge
      # from the list it describes is how the two drift.
      binOf = p:
        let n = p.pname or p.name; in
        if n == "beads" then "bd" else if n == "yq-go" then "yq" else n;

      metadataFor = pkgs: builtins.toJSON (map (p: {
        name = binOf p;
        package = p.pname or p.name;
        version = p.version or "";
        spdx = spdxOf p;
      }) (imageToolchainFor pkgs));
    in {
      packages = forAll (system:
        let pkgs = pkgsFor system; in {
          beads = beadsRelease pkgs;
          default = pkgs.buildEnv {
            name = "beadhive-local-install-toolchain";
            paths = toolchainFor pkgs;
          };
          image = pkgs.buildEnv {
            name = "beadhive-image-toolchain";
            paths = imageToolchainFor pkgs;
          };
          # A PACKAGE rather than a plain flake attribute, so `nix build .#metadata` resolves the
          # right system on its own. The alternative — `nix eval .#metadata.<system>` — needs the
          # system string spelled out or `--impure` to read builtins.currentSystem, and the docker
          # build would have to compute it. This just builds.
          metadata = pkgs.writeText "beadhive-toolchain-metadata.json" (metadataFor pkgs);
        });

      # `nix develop` for a shell with the toolchain on PATH — how install.sh drives it.
      devShells = forAll (system:
        let pkgs = pkgsFor system; in {
          default = pkgs.mkShell { packages = toolchainFor pkgs; };
        });
    };
}
