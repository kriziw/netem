# Live experience window

Tests default to a 15-second live experience window. Operators may select an integer from 5 to 120 seconds when starting a scenario, site test/plan or manual workload. The choice stays fixed during a test and is recorded in its report metadata. It does not rescale phases or alter configured application targets, statistical confirmation or hard deadlines.

The controller acknowledges a unique live phase identity on the executing simulator before applying the next phase. The simulator establishes the live lower boundary on its own clock on acknowledgement, then publishes once per second. Previous-phase cached results are withheld until the new identity is acknowledged. Short/new phases can therefore show a partial-duration, provisional window. Live display settling for transaction findings is two seconds; WAN-health/route smoothing remains eight seconds. Already-overdue in-flight operations can be reported without waiting for completion.

Pause freezes the live observation end; resume begins a fresh live segment, excluding paused observations. A borrowed workload is left running, with its pre-test live-window setting restored when the controller releases its context. Failure to establish the new phase fails the test rather than silently mixing evidence from different phases. Update the simulator companion together with this platform change.

Final phase results still query explicit full-phase start/end bounds and disjoint active intervals for pauses. They do not average the 15-second live snapshots or discard earlier transactions. Score coverage and confirmation safeguards remain unchanged; low traffic may still lack enough evidence for a confirmed result.
