"""Every Sentinel weight and threshold in one place (prd/04 SN-07). Frozen at the H13 tuning freeze; see README.md."""

# windows and baselines
WINDOW_S = 1.0
HISTORY_WINDOWS = 10            # rolling horizon for VERIFY completion, HOLD rate and timing
BASELINE_WINDOWS = 300          # EWMA horizon (5 minutes of NORMAL windows)

# which keys are candidates in a window
MIN_COUNT = 10                  # at least this many requests in the window (was 20: blind to slower clusters)
MIN_SHARE = 0.02                # and at least this share of all requests

# key score (weights sum to 1)
W_JUMP, JUMP_FULL = 0.30, 0.10  # share above baseline; 10 points above counts in full
W_UNKNOWN = 0.20                # 1 - known-client share
W_VERIFY = 0.20                 # 1 - VERIFY completion (0.5 when fewer than VERIFY_MIN prompts)
VERIFY_MIN = 5
W_REGULAR, REGULAR_CV = 0.15, 0.30   # per-IP gaps with coefficient of variation below this look scripted
REGULAR_MIN_IPS = 5
W_ASN, ASN_FLOOR, ASN_SPAN = 0.15, 0.15, 0.35  # top-network share above 15% starts to count, in full at 50%
BAD_SCORE = 0.60                # a window is "bad" for a key at or above this score

# customer protection (prd/04 section 4.2 step 3)
PROTECTED_KNOWN_SHARE = 0.50    # a fingerprint used mostly by known-good customers is never acted on as a whole
LEARN_MIN_COUNT = 3             # a fingerprint's normal customer mix is learned from any calm window with this many
                                # requests, so protection doesn't depend on the traffic rate (MIN_COUNT is for acting)

# escalation with hysteresis (step 5)
ENTER_BAD = 3                   # throttle after 3 bad windows
STEPUP_AFTER = 5                # then step-up after 5 more
BLOCK_AFTER = 5                 # then block after 5 more (never for protected keys)
RELEASE_GOOD = 10               # release after 10 good windows
UNTIL_S = 60.0

# load level (step 4)
COORDINATED_SHARE = 0.10
SURGE_FACTOR = 2.0
SURGE_MIN_FPS = 20
SURGE_MIN_KNOWN = 0.60
