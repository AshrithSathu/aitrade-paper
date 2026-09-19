import { defineRailway, postgres, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const Postgres = postgres("Postgres", { region: "us-west2" });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const paperVolume = volume("paper-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "us-west2", sizeMB: 50000 });
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "us-west2", sizeMB: 50000 });
  const paper = service("paper", {
    replicas: { "us-west2": 1 },
    deploy: { limitOverride: { containers: { cpu: 4, memoryBytes: 8000000000 } } },
    volumeMounts: { "/app/data": paperVolume },
    env: { DATABASE_URL: preserve(), PUBLIC_ORIGIN: preserve(), TUNNEL_TOKEN: preserve(), AI_GATEWAY_API_KEY: preserve() },
  });

  return project("aitrade-paper", {
    resources: [Postgres, paper, paperVolume, postgresVolume],
  });
});
