# Score scale and criticality sensitivity

Deterministic policy analysis, not live performance measurements. The CSV covers every medium business-critical industry/sub-industry/function combination and each configured critical application. All other applications are assumed healthy and the selected application is completely unavailable, with sufficient evidence. Standard/important/critical factors 1/2/4 remain the approved defaults; alternatives below are comparisons only.

| Site | Failed critical application | Workload share | Equal weights | Approved 1/2/4 | Comparison 1/2/8 | Acceptance |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Grocery · Headquarters / campus · Medium · Business-critical | wms_scan | 0.04% | 99.96 | 99.89 | 99.78 | fail |
| Home improvement · Headquarters / campus · Medium · Business-critical | wms_scan | 0.05% | 99.95 | 99.86 | 99.73 | fail |
| Insurance · Headquarters / campus · Medium · Business-critical | erp | 0.088% | 99.91 | 99.85 | 99.82 | fail |
| Hospital network · Headquarters / campus · Medium · Business-critical | pacs_imaging | 0.07% | 99.93 | 99.83 | 99.71 | fail |
| Parcel & courier · Headquarters / campus · Medium · Business-critical | voice | 5.756% | 94.24 | 86.02 | 75.63 | fail |
| Parcel & courier · Headquarters / campus · Medium · Business-critical | erp | 0.072% | 99.93 | 99.83 | 99.7 | fail |
| Parcel & courier · Headquarters / campus · Medium · Business-critical | wms_scan | 0.251% | 99.75 | 99.39 | 98.94 | fail |

Across 322 critical-application outage cases, the approved weighted score ranges from 4.55 to 99.89. A high overall utility number is therefore not a business acceptance pass. Every confirmed critical outage fails acceptance independently of utility.

## Legacy versus v2 fixtures

| Fixture | Legacy | V2 | Meaning |
| --- | ---: | ---: | --- |
| Reported-symptom illustration | 0.0 | 99.04 | Elastic utility; separate compliance |
| 20 requests at 301 ms / 300 ms target | 0.0 | 99.67 | Elastic utility; separate compliance |
| 20 requests at 600 ms / 300 ms target | 0.0 | 50.0 | Elastic utility; separate compliance |

The 70/80/90 site utility floors remain explicit policy choices. They are not numerically equivalent to legacy thresholds. A uniform elastic latency-only score of 80 corresponds to 1.25 times the target; P95 compliance can fail sooner, independently. The 113-transaction fixture is illustrative and cannot reconstruct the user's deployed application mix from aggregate numbers.

With incomplete coverage, the headline is provisional, the configured weighted coverage and possible full-score interval are returned, and acceptance cannot pass. Pending overdue work reports at-risk status. Raw metrics and small-sample misses remain observations; confirmed violations require the documented statistical rule.

See criticality-sensitivity.csv for every case. No empirical threshold recalibration is claimed without the unavailable lab.
