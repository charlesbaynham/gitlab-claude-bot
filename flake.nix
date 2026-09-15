{
  description = "gitlab-claude-bot: polls GitLab and runs Claude Code in a Docker container per job, packaged as a Proxmox LXC cattle template";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    cattle.url = "git+https://github.com/charlesbaynham/nix-proxmox-cattle?ref=v1";
  };

  outputs = { self, nixpkgs, cattle }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      py = pkgs.python3Packages;
      pyproject = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;

      gitlab-claude-bot = py.buildPythonApplication {
        pname = pyproject.name;
        version = pyproject.version;
        pyproject = true;
        src = ./.;
        build-system = [ py.hatchling ];
        dependencies = [ py.httpx ];
        nativeCheckInputs = [ py.pytestCheckHook pkgs.git ];
      };
    in
    nixpkgs.lib.recursiveUpdate
      {
        packages.${system} = {
          inherit gitlab-claude-bot;
          default = gitlab-claude-bot;
        };
        nixosModules.default = import ./nix/gitlab-claude-bot.nix;
      }
      (cattle.lib.mkTemplate {
        inherit nixpkgs system;
        name = "gitlab-claude-bot";
        stateDir = "/data";
        modules = [
          self.nixosModules.default
          {
            services.gitlab-claude-bot = {
              enable = true;
              package = gitlab-claude-bot;
              allowedUsers = [ "charlesbaynham" ];
            };
          }
        ];
      });
}
