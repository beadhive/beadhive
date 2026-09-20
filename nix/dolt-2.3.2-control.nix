let
  flake = builtins.getFlake (toString ../.);
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
in
pkgs.stdenvNoCC.mkDerivation {
  name = "dolt-2.3.2-control";
  src = pkgs.fetchurl {
    url = "https://github.com/dolthub/dolt/releases/download/v2.3.2/dolt-linux-amd64.tar.gz";
    hash = "sha256-eilJ+isrN5nuHlfm1kUZqNZdZ1/YMvZGnU4H5aHHKxQ=";
  };
  sourceRoot = "dolt-linux-amd64";
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -R . "$out/"
    runHook postInstall
  '';
}
