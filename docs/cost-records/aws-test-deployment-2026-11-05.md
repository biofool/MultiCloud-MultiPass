# Cost record: AWS EC2 GPU test deployment (ai-pentest-eval)

**Date:** 2026-11-05
**Account:** AWS account `858614766442`, IAM user `kkron`, profile `ai-pentest-test`
**Region:** us-east-1
**Root config:** `terraform/aws-test/` (module: `terraform/modules/aws_ec2/`)
**Status:** Plan ready (1 instance to add). Instance not created. Actual test cost: $0 so far.

## 1. Test resources and unit prices

| Resource | Spec | Unit price (us-east-1, on-demand) | Source |
|---|---|---|---|
| EC2 instance | `g6.xlarge` (1× L4, 24 GB VRAM) | $0.8048 / hr | AWS Pricing API, fetched 2026-11-05 |
| Root volume | gp3, 100 GB, encrypted | $0.08 / GB-month = $0.0110 / hr | AWS Pricing API, fetched 2026-11-05 |
| Public IPv4 | in-use address, 1 | $0.005 / hr | AWS published rate (since Feb 2024). **Not verified via Pricing API; the query returned no match.** |
| Security group, IAM role, instance profile | — | $0 | AWS |
| Data transfer | Model download plus test traffic | Not estimated. First 100 GB out per month is free; $0.09/GB beyond that. | AWS |

**Hourly total while running:** $0.8048 + $0.0110 + $0.005 = **$0.821 / hr**

## 2. Estimate for test durations

Costs are on-demand and assume the instance runs continuously. Stopping the instance removes the compute charge, but EBS and the public IPv4 keep billing.

| Duration | Compute | Storage (100 GB gp3) | Public IPv4 | Total |
|---|---|---|---|---|
| 1 hour | $0.80 | $0.01 | $0.01 | **$0.82** |
| 4 hours | $3.22 | $0.04 | $0.02 | **$3.28** |
| 8 hours | $6.44 | $0.09 | $0.04 | **$6.57** |
| 24 hours | $19.32 | $0.26 | $0.12 | **$19.70** |
| 730 hours (1 month, 24/7) | $587.5 | $8.00 | $3.65 | **$599.2** |

Note: `tofu destroy` deletes the root volume (`DeleteOnTermination` defaults to true), so storage and IPv4 charges stop at destroy.

## 3. Comparison with existing estimates

| Source | Figure | Relation to this estimate |
|---|---|---|
| `CloudManagement/docs/ec2-ai-workload-plan.md` | $0.805/hr; $588/mo (24/7) | Compute only. Live price $0.8048 matches. Excludes storage and IPv4. |
| `ai-pentest-eval/docs/aws-deployment-authorization.md`, Profile B | ~$604/mo (GPU + local models) | Plausible match. This estimate is $599/mo 24/7, excluding data transfer. Profile B's basis is not shown in this record. |
| Plan's note on a 100 GB model volume | ~$8/mo | Matches gp3 cost here, for the root volume at 100 GB. |

The live compute price matches the plan's figure. Total 24/7 cost is about $11/mo higher than the plan's compute-only figure, because of storage and IPv4.

## 4. Existing spend in the account (not from this test)

Cost Explorer, last 3 days, grouped by service:

| Service | USD |
|---|---|
| Amazon Elastic Compute Cloud - Compute | 14.57 |
| Amazon Virtual Private Cloud | 18.72 |
| EC2 - Other | 9.62 |
| Amazon Simple Storage Service | 1.60 |
| **Total** | **44.51** |

No instances were running at the time of the query. The test has created no billed resources, so this spend predates the test. The source of the VPC and EC2-Other charges is not identified. Check them before using this account as a baseline.

## 5. Actual cost (fill in after destroy)

| Item | Value |
|---|---|
| Apply time (UTC) | _pending_ |
| Destroy time (UTC) | _pending_ |
| Hours running | _pending_ |
| Actual test cost from Cost Explorer (filter tag `project_id=ai-pentest-eval-test`) | _pending_ |
| Variance vs. 1-hour estimate | _pending_ |

Cost Explorer may lag up to 24 hours, and tags must be activated as cost-allocation tags before they filter spend.

## 6. Sources

- AWS Pricing API, `AmazonEC2`, `g6.xlarge`, Linux, Shared tenancy, us-east-1 (queried 2026-11-05)
- AWS Pricing API, `Storage`, `gp3`, us-east-1 (queried 2026-11-05)
- `CloudManagement/docs/ec2-ai-workload-plan.md`, Section 2
- `ai-pentest-eval/docs/aws-deployment-authorization.md`, Sections 1 and 7.2
