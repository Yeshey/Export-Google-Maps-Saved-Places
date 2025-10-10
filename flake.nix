{
  description = "Google Takeout CSV to GPX converter";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        pythonWithPackages = pkgs.python3.withPackages (ps: with ps; [
          playwright
        ]);
        
        scriptWrapper = pkgs.writeShellScriptBin "csv2gpx" ''
          export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
          export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
          ${pythonWithPackages}/bin/python3 ${./main.py} "$@"
        '';
        
      in
      {
        packages.default = scriptWrapper;
        
        apps.default = {
          type = "app";
          program = "${scriptWrapper}/bin/csv2gpx";
        };
        
        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonWithPackages
            pkgs.playwright-driver.browsers
          ];
          
          shellHook = ''
            export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
            export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
          '';
        };
      }
    );
}