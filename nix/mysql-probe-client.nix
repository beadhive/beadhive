let
  flake = builtins.getFlake (toString ../.);
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
in
pkgs.python3.withPackages (pythonPackages: [ pythonPackages.pymysql ])
