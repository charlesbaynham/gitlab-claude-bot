# gitlab-claude-bot as a hardened systemd service beside a Docker daemon: the
# daemon polls GitLab and runs each job in a throwaway container from
# agentImage, with a per-job checkout bind-mounted from the state volume.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.gitlab-claude-bot;
  user = "gitlab-claude-bot";
  homeDir = "${cfg.stateDir}/${user}";
  workDir = "${homeDir}/work";
  botStateDir = "${homeDir}/state";
  secretsFile = "${cfg.stateDir}/secrets/${user}.env";
  docker = config.virtualisation.docker.package;

  # Values are already in the environment via EnvironmentFile when this runs.
  checkSecretsScript = pkgs.writeShellScript "gitlab-claude-bot-check-secrets" ''
    set -eu
    forge=
    for var in GITLAB_TOKEN GITHUB_TOKEN; do
      val="$(eval printf '%s' "\''${$var:-}")"
      if [ "$val" = CHANGEME ]; then
        echo "gitlab-claude-bot: $var is still a placeholder in ${secretsFile}" >&2
        exit 1
      fi
      if [ -n "$val" ]; then forge=1; fi
    done
    if [ -z "$forge" ]; then
      echo "gitlab-claude-bot: set GITLAB_TOKEN, GITHUB_TOKEN or both in ${secretsFile}" >&2
      exit 1
    fi
    for var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY; do
      val="$(eval printf '%s' "\$$var")"
      if [ -n "$val" ] && [ "$val" != CHANGEME ]; then exit 0; fi
    done
    echo "gitlab-claude-bot: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in ${secretsFile}" >&2
    exit 1
  '';
in
{
  options.services.gitlab-claude-bot = {
    enable = lib.mkEnableOption "the GitLab Claude bot";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The gitlab-claude-bot Python application derivation.";
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/data";
      description = "Mountpoint holding the bot's state, its per-job checkouts, the Docker image store and the secrets file.";
    };

    agentImage = lib.mkOption {
      type = lib.types.str;
      default = "ghcr.io/charlesbaynham/gitlab-claude-bot-agent:latest";
      description = "Docker image run once per job. Pin a digest for reproducible deploys.";
    };

    allowedUsers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Usernames whose assignments, mentions and comments the bot acts on, on every forge. Empty leaves ALLOWED_USERS (and GITLAB_/GITHUB_ALLOWED_USERS) to the secrets file.";
    };

    pollInterval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 30;
      description = "Seconds between polls.";
    };

    jobTimeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1800;
      description = "Seconds a single job may run before its container is killed.";
    };

    maxBudgetUsd = lib.mkOption {
      type = lib.types.nullOr lib.types.float;
      default = null;
      description = "Per-job spend cap passed to Claude Code, or null for none.";
    };

    healthPort = lib.mkOption {
      type = lib.types.port;
      default = 8000;
      description = "Port of the plain-HTTP health endpoint.";
    };

    allowedHealthSources = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Hosts allowed to reach healthPort. Empty opens it to all; a deployer that health-checks the port must be listed.";
    };
  };

  config = lib.mkIf cfg.enable {
    # overlay2 explicitly, so a silent fall back to vfs fails the boot instead of
    # eating the volume; data-root on the state volume so the agent image survives redeploys.
    virtualisation.docker = {
      enable = true;
      storageDriver = "overlay2";
      daemon.settings.data-root = "${cfg.stateDir}/docker";
      autoPrune = {
        enable = true;
        dates = "weekly";
      };
    };

    # uid 1000 must equal the agent container's uid, so the daemon can commit what
    # the agent wrote into the bind mount.
    users.groups.${user}.gid = 1000;
    users.users.${user} = {
      isSystemUser = true;
      uid = 1000;
      group = user;
      home = homeDir;
      extraGroups = [ "docker" ];
    };

    # ReadWritePaths must exist before the unit's mount namespace is built,
    # which is before any ExecStartPre — so tmpfiles, at boot.
    systemd.tmpfiles.rules = [
      "d ${homeDir} 0750 ${user} ${user} -"
      "d ${workDir} 0700 ${user} ${user} -"
      "d ${botStateDir} 0700 ${user} ${user} -"
      "d ${cfg.stateDir}/docker 0710 root root -"
    ];

    systemd.services.gitlab-claude-bot-agent-pull = {
      description = "Pull the gitlab-claude-bot agent image";
      after = [ "network-online.target" "docker.service" ];
      wants = [ "network-online.target" ];
      requires = [ "docker.service" ];
      before = [ "gitlab-claude-bot.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${docker}/bin/docker pull ${cfg.agentImage}";
        Restart = "on-failure";
        RestartSec = 30;
      };
    };

    systemd.services.gitlab-claude-bot = {
      description = "GitLab Claude bot";
      after = [
        "network-online.target"
        "docker.service"
        "cattle-state-preflight.service"
        "gitlab-claude-bot-agent-pull.service"
      ];
      wants = [ "network-online.target" ];
      requires = [ "docker.service" "cattle-state-preflight.service" ];
      wantedBy = [ "multi-user.target" ];

      path = [ pkgs.git docker ];

      # Non-secret configuration; the tokens come from EnvironmentFile only.
      environment = {
        HOME = homeDir;
        GITLAB_URL = "https://gitlab.com";
        STATE_DIR = botStateDir;
        WORK_DIR = workDir;
        AGENT_IMAGE = cfg.agentImage;
        POLL_INTERVAL = toString cfg.pollInterval;
        JOB_TIMEOUT = toString cfg.jobTimeout;
        HEALTH_PORT = toString cfg.healthPort;
      } // lib.optionalAttrs (cfg.allowedUsers != [ ]) {
        ALLOWED_USERS = lib.concatStringsSep "," cfg.allowedUsers;
      } // lib.optionalAttrs (cfg.maxBudgetUsd != null) {
        MAX_BUDGET_USD = toString cfg.maxBudgetUsd;
      };

      serviceConfig = {
        Type = "simple";
        User = user;
        Group = user;

        # Mode 0600, seeded out of band; a missing file fails the unit outright.
        EnvironmentFile = secretsFile;
        ExecStartPre = [ "${checkSecretsScript}" ];
        ExecStart = "${cfg.package}/bin/gitlab-claude-bot";

        Restart = "on-failure";
        RestartSec = 10;
        KillMode = "mixed";
        TimeoutStopSec = 60;

        ProtectSystem = "strict";
        ProtectHome = true;
        # PrivateTmp is why WORK_DIR lives under the state dir: dockerd cannot see this unit's /tmp.
        PrivateTmp = true;
        NoNewPrivileges = true;
        ReadWritePaths = [ homeDir ];
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        # An allow-list group, never `~@privileged`: that silently subtracts @chown
        # and SIGSYS-kills the process (the lab's 2026-09-12 lesson).
        SystemCallFilter = [ "@system-service" ];
        SystemCallErrorNumber = "EPERM";
      };
    };

    networking.firewall = lib.mkMerge [
      (lib.mkIf (cfg.allowedHealthSources == [ ]) {
        allowedTCPPorts = [ cfg.healthPort ];
      })
      (lib.mkIf (cfg.allowedHealthSources != [ ]) {
        extraCommands = lib.concatMapStringsSep "\n"
          (src: "iptables -A nixos-fw -p tcp -s ${src} --dport ${toString cfg.healthPort} -j nixos-fw-accept")
          cfg.allowedHealthSources;
      })
    ];
  };
}
