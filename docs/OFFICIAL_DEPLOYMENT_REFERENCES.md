# Official deployment and exchange references

Verified for this release on 19 July 2026:

## GitHub

- [Deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments): environment secrets, required reviewers, branch/tag restrictions, and protection rules.
- [Deploying with GitHub Actions](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments): environments and concurrency for a single in-progress deployment.
- [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use): pin third-party actions to full-length commit SHAs. This repository does so.
- [Actions secrets reference](https://docs.github.com/en/actions/reference/security/secrets): keep SSH material and host identity in secrets, never in the repository.

## Oracle Cloud Infrastructure

- [OCI Free Tier](https://docs.oracle.com/iaas/Content/FreeTier/freetier.htm) and [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm): current A1 allocation and home-region rules.
- [OCI security rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm): Oracle recommends network security groups for per-VNIC policy.
- [OCI security lists](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securitylists.htm): OCI virtual firewall behavior and the separate instance firewall requirement.

Use an A1 Flex VM with at least 2 OCPUs and 12 GiB RAM for the declared
four-bot shared host, plus 4 GiB swap and at least 80 GiB free root storage. Expose only
SSH from a trusted `/32`; the bot, Freqtrade API, and monitoring ports remain
loopback/private and require no public ingress rule.

## Binance Spot

- [Spot Testnet WebSocket API](https://developers.binance.com/en/docs/products/spot/testnet/web-socket-api): testnet endpoint, 24-hour connection lifetime, ping/pong behavior, and API-key separation.
- [User Data Stream signature subscription](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-api/user-data-stream): `userDataStream.subscribe.signature` and its signed parameters.
- [Spot REST general endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general): official `/api/v3/ping`, server time, and exchange-information endpoints.

The testnet package must complete the official Spot Testnet lifecycle and Oracle
soak before the live package's evidence gate can be satisfied.
