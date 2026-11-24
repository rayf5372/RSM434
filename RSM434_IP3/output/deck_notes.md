# Deck notes

Paste-ready paragraphs for the dry run. Figures match `ip3_compiled.xlsx`. Sharpe is excess over T-bills. Drawdowns and CVaRs are losses. A positive signed return is a gain.

## How the numbers are built

The panel is 218 months, Sep 2008 to Oct 2026. Each return sits next to the regime at the previous month-end. Annualized return is geometric. Sharpe uses the T-bill return known at the start of the month. Max drawdown compounds the months in that bucket in calendar order, so it is the track record of being in the strategy only in those months. Beta is against the S&P 500 total return. Buy-writes and the collar hold the equity. PUT, CNDR and BFLY hold T-bills. The indices have no trading costs, fees or taxes, and the history before each launch date is back-calculated.

## Full sample

The S&P returned 12.4% annualized with a Sharpe of 0.74, a maximum drawdown of 41.8% and a 95% CVaR of 9.9%. PPUT is the closest strategy on Sharpe (0.72), with a drawdown of 23.6%, a CVaR of 7.3% and a beta of 0.69. CLL has a drawdown of 21.6% and a CVaR of 6.0% at a Sharpe of 0.65 and a beta of 0.63. The buy-writes cut beta (BXM 0.63, BXMD 0.80) and lag the market's Sharpe (BXM 0.49, BXMD 0.63). PUT, which is short an at-the-money put against T-bills, has a Sharpe of 0.59 and a beta of 0.61. CNDR's Sharpe is -0.07 with a beta of 0.09. BFLY lost 2.4% annualized, Sharpe -0.28, and its stitched drawdown of 53.4% is deeper than the S&P's, on a beta of 0.05. That drawdown is the accumulated bleed in ordinary months.

## Low/Falling, calm (85 months)

Income is the book that earns its keep when VIX is below its trailing median and falling. PUT returned 7.5% with a Sharpe of 1.01, a beta of 0.44, a stitched drawdown of 9.4% and a CVaR of 4.1%. The S&P in the same months returned 6.6%, Sharpe 0.49, drawdown 20.8%, CVaR 6.8%. BXM's Sharpe is 0.66 with a beta of 0.50; BXMD and BXY are in the same range and carry more equity. PPUT's Sharpe is 0.23 and CLL's is 0.31: the premium spent on protection is visible in the average. Both halves agree on the sign for PUT (Sharpe 1.59 then 0.45) and for the S&P (0.73 then 0.25). BFLY does not: Sharpe 0.43 in the first half and -0.30 in the second. The worst calm-regime month for the equity books is Sep 2008, which is the month the credit modifier is there to catch. On this record, lean toward PUT and then the buy-writes, and leave the protection books and BFLY off.

## Low/Rising, waking up (34 months)

The S&P is the strongest book: 17.3% annualized, Sharpe 1.23. BXMD (1.08) and BXY (1.04) stay close. PPUT (1.06) and CLL (0.94) keep up with the market's Sharpe. PUT falls to 0.71: an at-the-money put sale lags a market that is still rising. CNDR's Sharpe is -0.45 (-0.01 then -1.26 across the halves) and BFLY's is -0.85 (-0.80 then -0.88). The halves have 20 and 14 months, so the ranking among the positive Sharpes moves, while the sign of those Sharpes does not. The usable split is equity or a light out-of-the-money overwrite, and no range sale.

## High/Rising, stress (68 months)

Average returns stay positive. The S&P returned 14.7% with a Sharpe of 0.71. The reason to change the book is the tail: stitched drawdown 31.0%, CVaR 12.3%, worst month Oct 2008 at -16.8%. PPUT improves the average and the tail together: Sharpe 1.01, return 15.4%, drawdown 9.7%, CVaR 6.9%, beta 0.56. The Sharpe is 0.95 in the first half and 1.04 in the second. CLL cuts the CVaR to 5.9% and the drawdown to 13.3%, and the average falls to a Sharpe of 0.50 on a 7.2% return. Buy-write betas are 0.72 to 0.85, with drawdowns near 31.9% and CVaRs near 12.1%. PUT's worst month is Oct 2008 at -17.7%, and its Sharpe is 0.26. CNDR's Sharpe is -0.42 (-0.46 in the first half, -0.38 in the second). BFLY's is -0.55 (-0.61, then -0.50). No strategy in this regime changes the sign of its excess return between halves. Lean toward PPUT when the aim is return and tail together, and toward CLL when the aim is the worst month. Leave CNDR, BFLY and the put-write.

When the same regime arrives with widening credit (39 months), PPUT's Sharpe is 1.24 and the S&P's CVaR is 14.6%. PUT's Sharpe is -0.03 and its drawdown is 31.1%. With tightening credit (29 months) PUT's Sharpe is 0.88, in line with the S&P's 0.88. The stress book gets stricter while spreads are widening, and income is ordinary again while they are healing.

## High/Falling, recovery (31 months)

This is the smallest volatility bucket: 13 months in the first half and 18 in the second. The S&P returned 18.7%, Sharpe 1.06, and the Sharpe stays positive in both halves (1.77, then 0.60). CLL has the highest full-sample Sharpe in the bucket, 1.65, with a drawdown of 6.2% and a beta of 0.51, and it is positive in both halves (1.54, then 1.69). Its rank still moves from seventh to first, so 1.65 is a noisy edge. PUT's Sharpe is 1.37 overall and 4.09 then 0.43. CNDR goes from 2.61 to -0.18, and BFLY from 0.73 to -0.31. PPUT's Sharpe is 0.81, and its worst month in the bucket is Jan 2009 at -9.8%, against the S&P's worst of -9.0% in Dec 2018. Hold equity beta in a recovery. Do not rank CNDR or BFLY off this cell, and treat a 95% VaR from 31 months as a description of a few bad months.

## Credit as the added signal

Inside Low/Falling, tightening credit (63 months) lines up with an S&P return of 13.5% and a Sharpe of 1.16. Widening credit (22 months) lines up with an S&P return of -11.1% and a Sharpe of -1.02. That widening cell is the reading in front of both Sep 2008 and Jan 2022. It does not repeat at the same size: the S&P averaged -1.9% a month in the first half of the sample (10 months) and -0.1% a month in the second (12 months). Use it as a reason to cut equity-collateral income when volatility looks calm and spreads are widening, and keep the confidence low. Low/Rising with widening spreads is 5 months and is set aside. High/Falling with tightening spreads is 12 months and an S&P return of 47.4%, which is a small-sample rebound.

The 2s5s slope leaves High/Rising equity returns almost unchanged (14.8% when the curve is flattening, 14.6% when it is steepening). It does separate Low/Falling (12.7% steepening versus 0.6% flattening). Credit is the added signal because it is the one that changes the stress decision, which is where the strategy ranking moves.

## Volmageddon, 26 Jan to 8 Feb 2018

The S&P fell 10.1% and VIX reached 37.3 on 5 Feb. Every strategy lost, and the range was tight: BXM -7.7%, BXMD -8.5%, BXY -9.1%, PUT -7.6%, CNDR -4.7%, BFLY -3.8%, CLL -7.8%, PPUT -6.8%, SPX_TR -10.1%. The defined-risk books lost less. A 5% out-of-the-money put cushioned the index only partly, over a two-week window.

On 29 Dec 2017 the reading was High/Falling, VIX 11.0 against a trailing median of 10.8, credit tightening. A VIX of 11.0 is a low level. The High label records that it sat just above that median, and credit was tightening. On 25 Jan the same rules read High/Rising, VIX 11.6, one-month change +0.5 points. The level was still about 12. The January month-end falls inside the window, so it is concurrent. Filtered HMM P(Stress) on the December month-end was 0.00, state Calm.

## COVID crash, 19 Feb to 23 Mar 2020

The S&P fell 33.8%. VIX peaked at 82.7 on 16 Mar. Buy-writes and the put-write fell with the equity: BXM -30.2%, BXMD -32.3%, BXY -31.4%, PUT -28.9%, CNDR -9.2%, BFLY -3.2%, CLL -14.9%, PPUT -11.8%, SPX_TR -33.8%. CLL, PPUT, CNDR and BFLY are the books whose wings or outright put were built for this path.

On 31 Jan 2020 the reading was already High/Rising, VIX 18.8 against a median of 14.5. Credit was tightening, so the volatility rule had flipped and the credit modifier had not. On 18 Feb, VIX was 14.8 and the same rules read High/Falling, one-month change -4.0 points. The January month-end was the warning, and by the eve it had faded. Filtered HMM P(Stress) on 31 Jan was 0.00, state Calm. The HMM moved to Stress in February, which is inside the window.

## 2022 bear market, 3 Jan to 12 Oct

The S&P fell 24.5% and VIX reached 36.5 on 7 Mar. The path was a grind. PPUT fell 21.7%, close to the equity, because a one-month 5% put rolled through a slow decline still leaves the strategy long the S&P. CLL fell 17.1%, the buy-writes between 17.2% and 22.0%, PUT 13.7%, CNDR 5.6% and BFLY 4.9%. The defined-risk books held up. The rolling crash hedge did not. Full path: BXM -17.2%, BXMD -22.0%, BXY -21.3%, PUT -13.7%, CNDR -5.6%, BFLY -4.9%, CLL -17.1%, PPUT -21.7%, SPX_TR -24.5%.

On 31 Dec 2021 the reading was Low/Falling: VIX 17.2 was below its median of 18.7, and credit was widening. The volatility grid was quiet. Credit was the signal that had flipped. The window starts on the first session of January, so the eve reading is that same month-end. Filtered HMM P(Stress) was 0.00, state Transition.

## Financial crisis, 15 Sep 2008 to 9 Mar 2009

This is the deep bear the other three windows are not, and the spread across strategies is about 40 percentage points. The S&P fell 42.4%, the drawdown inside the window is 45.3%, and VIX reached 80.9 on 20 Nov. CNDR fell 2.1% and BFLY 4.9%. PUT fell 31.1%: the same T-bill collateral as the condor, short an uncapped crash. Buy-writes fell 33.1% to 38.0%. CLL fell 21.5% and PPUT 23.3%. Both hold the S&P, so the put reduced the loss and a large equity loss remained. The condor's wings are the difference between CNDR and PUT. Full path: BXM -33.1%, BXMD -38.0%, BXY -36.2%, PUT -31.1%, CNDR -2.1%, BFLY -4.9%, CLL -21.5%, PPUT -23.3%, SPX_TR -42.4%.

On 29 Aug 2008 the reading was Low/Falling, VIX 20.6 below a median of 22.8, credit widening. The relative volatility rule was quiet because the median had already risen, and credit had widened. On 12 Sep, before the window opens, the same rules read High/Rising, VIX 25.7, up 5.0 points from the August month-end, credit still widening. A check between month-ends would have flipped. A process that waits for month-end would have carried Low/Falling into the week of 15 Sep. Filtered HMM P(Stress) on 29 Aug is 1.00, and that date is the first month of the HMM sample. The estimated initial distribution is entirely Stress, so the August probability is the starting state, not a warning.

## What the hidden Markov model adds

The model has three Gaussian states on log VIX, the one-month VIX change and the three-month credit change, standardized on the full sample. Ordered by average VIX they are Calm (15), Transition (21) and Stress (34). The chance of staying in a state is high (the diagonal of the transition matrix is about 0.87 to 0.92). The Viterbi labels use the whole sample. The probabilities in the episodes above are filtered, so each one uses only data through that month, apart from the opening month noted there.

These states are a level of volatility. They are not the 2x2 grid. 19% of High/Rising months fall in HMM Stress, and 56% of Low/Falling months fall in HMM Calm. A VIX near 11 can be High on the rule, because the rule compares VIX with the last year, and Calm in the HMM, because 11 is a low level. That is the December 2017 label. Filtered P(Stress) was about zero on the month-end before Volmageddon, COVID and the 2022 window. On 31 Jan 2020 the rule was already High/Rising and the HMM was still Calm. The level model is a check on what High means. It did not turn earlier than the rules.

## Monitor, 2 Nov 2026

Latest saved close, not a live quote. VIX 21.15 against a trailing median of 17.18, change +1.73 since 30 Oct. High/Rising, credit widening, 2s5s flattening. The October month-end had the same volatility and credit reading. The history for the slide is the High/Rising block above, and the stricter cut is High/Rising with widening credit (39 months): PPUT Sharpe 1.24, PUT Sharpe -0.03.
