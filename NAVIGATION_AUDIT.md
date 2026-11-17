# Kite navigation audit

## Scope and outcome

The application has 13 top-level hash routes: `launch`, `home`, `learn`, `lesson`, `challenge`, `quiz`, `playground`, `progress`, `leaderboard`, `notifications`, `profile`, `preferences`, and `about`. The application renders these from the prototype documents in `web/index.html`; `web/assets/app.js` handles the API bridge and contextual actions, and `web/assets/playground-shortcut.js` handles the learning-screen Playground shortcut.

The audit found three route-access defects:

1. The original route guard treated `leaderboard` and `profile` as protected, and `/api/leaderboard` required an account. Existing uncommitted workspace changes remove that gate, make leaderboard access optional-user, and render guest profile/activity from device-local progress. Those changes were preserved.
2. An unrecognized hash rendered the launch document without correcting the URL. The bridge now replaces unrecognized hashes with `#launch`. Known routes and auth callback hashes remain distinct; login/signup/reset subroutes are passed into the launch iframe so direct links render their intended view.
3. A direct login/signup/reset subroute had no corresponding iframe hash, so the launch screen could remain on its welcome view. The bridge now forwards those known subroutes to the launch iframe.

No separate router was added. Existing hash/history navigation, per-screen actions, guest state, and authorization checks remain in use.

## Navigation element audit

“Before” describes the behavior found in the worktree before this task’s route-normalization change. Where an existing workspace change had already corrected a behavior, that is called out rather than attributed to this task.

| Navigation element | Intended action | Before | Correction / disposition | Final verification |
|---|---|---|---|---|
| Top-level route URLs and refresh | Open the requested existing screen and retain it after reload | All declared routes resolved; invalid hashes rendered Launch while leaving an invalid URL | Canonicalize unknown hashes to `#launch`; preserve callback parameters | **Pass:** all 13 routes opened directly and survived a refresh with the expected hash/title; invalid hash resolved to `#launch` |
| Launch auth subroutes: signup, login, reset, welcome | Show the selected launch/auth view on direct URL access | The outer route rendered Launch but did not pass the subroute hash into its iframe | Forward known subroute hashes into the launch iframe; leave OAuth/recovery callback parameters intact | **Intentionally excluded:** authentication journeys are not part of this audit. The application-wide route checks do not claim direct auth-subroute verification. |
| Home bottom navigation: Home, Learn, Progress | Open the corresponding top-level screen | Existing route mappings were correct | No mapping change | **Pass:** clicked each in the browser; expected route opened |
| Home “View All”, “Continue”, and Quick Access buttons | Open Learn, Lesson, Challenge, Quiz, Leaderboard, and Profile | Existing mappings were correct | No mapping change | **Pass:** clicked all six; each opened its intended route |
| More menu: Profile, Notifications, Leaderboard, Challenges, Quizzes, Settings, Help & About | Open the selected destination and close the menu | Existing mappings were correct | No mapping change | **Pass:** clicked all seven; route matched and menu closed |
| More menu: Sign In, Exit Guest Mode, Log Out | Enter sign-in, leave guest flow, or revoke a signed-in session | Actions were present; auth behavior is distinct from page navigation | Kept explicit sign-in/logout actions separate from ordinary navigation | **Partial:** visibility and destinations inspected; authenticated logout and session revocation covered by API tests, not browser-tested with a disposable signed-in account |
| Home hamburger drawer: My Profile, Achievements, Settings, Help & About | Open Profile, Progress/Achievements, Settings, and About | Existing mappings were correct | No mapping change | **Pass:** clicked each as a guest; expected destination opened without sign-in |
| Home header Notifications button | Open Notifications | Existing route is present | No mapping change | **Not individually clicked**; Notifications is reachable and tested through More |
| Learning Path lesson cards and lesson Back | Open the selected lesson; return to the prior screen | Existing `data-kite-lesson` and history behavior were correct | No mapping change | **Pass:** opened the first lesson, switched Lesson/Example/Quiz tabs, and Back returned to Learn |
| Lesson assessment “Start quiz”; quiz Return to lesson | Enter the lesson-specific quiz; return to the same lesson context | Existing context handoff was correct | No mapping change | **Pass:** lesson assessment opened Quiz; Return to lesson returned to the originating lesson |
| Quiz Previous/Next question controls | Move between questions without changing the route or losing answers | Existing answer gating and draft persistence were correct | No mapping change | **Pass:** selected an answer, advanced, returned to the previous question, and returned to the lesson |
| Challenge, assessment, and premium links | Open the selected task; request sign-in or Premium only for the gated action/content | Route access and authorization are separate; challenge submission requires an account and Premium content returns a separate entitlement response | Preserved the distinction; route viewing does not itself require sign-in | **Partial:** direct Challenge/Quiz routes render; backend tests cover guest quiz and free-user Premium denial. Signed-in free/Premium browser journeys were not exercised |
| Floating Playground shortcut and Playground Exit | Open Playground from learning and return to the originating screen/context | Existing context capture/history behavior was correct | No mapping change | **Pass:** opened Playground from Lesson and Exit returned to Lesson |
| Progress tabs: Progress, Achievements, Stats | Switch the visible subsection without changing route | Existing tab behavior was correct | No mapping change | **Pass:** each tab selected in place |
| Leaderboard tabs: Weekly, Monthly, All Time | Change the leaderboard period without requiring sign-in | Original route/API path required an account; current worktree changes allow guests to view public leaders without personal rank | Preserved guest endpoint and nullable personal rank; client shows a guest sign-in hint instead of dereferencing a missing rank | **Pass:** tabs switched in place; guest endpoint returned 200 and `current: null` |
| Notifications tabs and Back | Filter All/Updates/Reminders; return to prior screen | Existing category and history mappings were correct | No mapping change | **Pass:** each category tab selected in place; Back control is present. Back destination was not separately exercised |
| Profile: Back, Settings, My Activity, My Achievements | Return to prior screen, open Settings, or open the corresponding guest/account panel | Original route guard required sign-in; current worktree has guest profile data and local activity | Preserved guest profile and device-local progress/activity | **Pass:** guest profile showed device-local stats/activity; Settings opened without redirect. My Achievements panel was not individually clicked |
| Settings: profile/account controls, theme/language/notifications, subscription | Open the requested settings panel or change the preference; require auth only for account changes/purchase | Ordinary Settings route is separate from signed-in account actions | Preserved explicit account/purchase gates and guest theme/preference behavior | **Partial:** Settings and subscription state rendered as Guest/Free; purchase, account mutation, and Google Play flows were not submitted |
| About: Back, Terms, Privacy, Contact, FAQ | Show the selected legal/help content or return | Existing screen and legal handlers were present | No mapping change | **Partial:** About route rendered; individual legal/FAQ controls were not each activated |
| Desktop/tablet/mobile layouts | Keep navigation and menu usable within viewport | The prior audit checked selected navigation and Settings at 375×812, 768×1024, and 1440×900; the embedded screens otherwise retained a 420px phone shell | This responsive pass adds shared screen-width overrides and persistent wide-screen route navigation; see validation details below | **Partial:** desktop Home and Quiz rendered and the desktop route list was available. All routes, orientations, and device widths were not rechecked in this pass. |

## Responsive layout implementation and validation

The loader now applies a shared responsive stylesheet to each of the 13 embedded screen documents (`launch`, `home`, `learn`, `lesson`, `playground`, `challenge`, `quiz`, `progress`, `leaderboard`, `profile`, `preferences`, `notifications`, and `about`). It keeps the compact phone presentation on narrow screens, expands the content container for tablet and desktop widths, constrains wide reading areas, gives Home quick-access and Progress tiles additional columns, and presents the Playground editor beside its output console on sufficiently wide screens. Short landscape viewports use the available height instead of retaining the framed-phone margins.

Navigation is available through the existing mobile screen controls and More sheet, a compact route rail from 740–999px, and a full labeled sidebar from 1000px upward. The wide navigation links to every embedded screen and marks the active route for assistive technology. On wide layouts the redundant in-screen bottom navigation is hidden; it remains available below the route-rail breakpoint.

**Validation in this continuation:** the local app responded with HTTP 200 on the isolated port-8890 server. Using Computer Use, Chrome rendered Home and Quiz in a 1920×1038 desktop window, and the 13-route desktop navigation was exposed with Quiz content loaded. `git diff --check` and Python syntax checks passed.

**Limitations:** Chrome reported user input while window resizing was attempted, so responsive-device emulation was stopped rather than bypassed. The new mobile, tablet, short-landscape, wide-playground, modal, and overflow states have not been empirically checked at each breakpoint; this continuation does not claim all 13 screens are visually validated at every size. The earlier 375×812, 768×1024, and 1440×900 checks were selected Settings/navigation checks, not a route-by-route responsive test. Firefox, Safari/iOS, Android, and installed-app layouts were not tested.

## Session, API, and history checks

- Guest navigation to Home, Learn, Lesson, Quiz, Progress, Leaderboard, Profile, Notifications, Settings, and About did not redirect to sign-in.
- Guest `/api/leaderboard` and `/api/modules` returned `200`; leaderboard personal rank was `null`.
- `/api/auth/session` returns `401` when there is no guest session, and `/api/progress` remains account-only. These are expected authorization responses; guest screens use local progress and public modules instead. The browser reports the expected session-check 401 as a console resource error, but it does not interrupt navigation.
- Browser Back and Forward were exercised after Home → Learn → Lesson and returned to Learn → Lesson. Playground Exit returned to the lesson context.
- Direct access plus refresh was verified for each of the 13 top-level routes.
- At the time of the original navigation audit, the Python test suite passed 61 tests, including guest leaderboard, guest quizzes/challenges, session restoration/logout, curriculum examples/assessments, and Premium entitlement checks.
- `git diff --check` passed.

## Release verification limits

This environment has no configured Supabase/Google OAuth identity or native Google Play Billing bridge. The browser run did not create or alter a real user account, so authenticated-free versus Premium subscriber navigation, account logout in a browser, OAuth callbacks, purchase/restore, and purchase-management destinations remain release checks. These are not represented as browser-verified here; existing backend tests cover related API authorization and session behavior.

## Functional, content, and subscription audit

### Learning content

#### Availability investigation — 2026-11-16

The shared browser page was pointed at `http://127.0.0.1:8890`. Its lesson request failed with `ERR_CONNECTION_REFUSED` for `/api/lessons/python-variables`; the local test server had been stopped after the preceding responsive checks. The already-loaded lesson document therefore retained its pre-hydration sample while the API-backed lesson content could not be retrieved. This was a local service outage, not deletion or overwriting of curriculum data.

The recovery audit found 101 non-empty lesson rows in the local `kite.db`. Every lesson ID, title, description, and content body matched the current `backend/curriculum.py` source exactly; content lengths range from 2,287 to 4,590 characters. There were no missing or extra lesson rows and no SQLite foreign-key violations. The existing progress row still references a valid lesson.

Quiz and challenge definitions are source-controlled data, not database catalog rows: `backend/curriculum.py` and `backend/app.py` define 25 module quizzes (130 questions), 13 lesson-specific quizzes (52 questions), and 80 challenges (127 test cases). The database stores quiz attempt/results history; it does not store quiz prompts or challenge definitions. Startup seeds/updates the lesson table from the source catalog and does not drop lesson or progress data.

For end-to-end verification, the API was temporarily started on loopback with an isolated SQLite database, leaving the existing `kite.db` untouched. Health, module, lesson, quiz, and challenge endpoints responded successfully; the browser displayed the full first lesson, its mapped 10-question quiz, and the challenge catalog. A guest quiz submission scored 10/10. The frontend now clears pre-hydration sample content and presents an accessible Retry action if lesson, module, quiz, or challenge catalog loading fails; browser checks confirmed the missing-content state and successful recovery after restoring the API route. Eleven targeted regression tests passed, including module/lesson counts, lesson examples and assessments, quiz-to-lesson/challenge mappings, lesson and quiz progress persistence, guest quiz submission, and runner-backed challenge submission with the runner mocked. The local challenge execution service itself is not part of this verification.

No content recreation or database restore was needed. The production Supabase/Vercel database was not queried, so this finding establishes the local repository/database state and the shared localhost failure, not the current contents of a remote deployment.

| Area | Finding and verification | Status |
|---|---|---|
| Learning modules and progression | 12 modules contain 101 lessons (8 lessons in each of 11 modules and 13 in Advanced). The module sequence progresses from Python foundations and types through control flow and data structures to files, errors, OOP, and advanced topics. | **FULLY FUNCTIONAL** for catalog and lesson delivery |
| Lesson content | Every lesson has substantial explanatory content (2,287–4,590 characters in the current catalog), a worked example, walkthrough, expected output, practice, and an assessment. All 101 worked examples are distinct. The curriculum test executes/checks the examples and verifies contextual assessments. | **FULLY POPULATED**; quality is template-supported and should continue to receive educator review |
| Repeated lesson boilerplate | Replaced the tautological generic definition with each lesson's topic objective, improved the step-by-step explanation, made the result explanation include that example's expected output, and varied the second practice prompt. Lesson IDs, examples, expected outputs, and progression were preserved. | **FIXED and regression-tested** |
| Quizzes and challenges | The API exposes 25 module quizzes plus 13 lesson-specific quizzes and 80 challenges. The 25 module quizzes contain 130 questions; the lesson assessments contain 52 more. Quiz/topic/lesson/challenge relationships are covered by API tests. | **CATALOGS POPULATED:** quiz submissions are verified; challenge execution depends on the separately deployed runner |

### Subscription and billing

The subscription dashboard is part of Settings rather than a separate route. It presents monthly (K19.99) and annual (K99.99) plans, plan benefits, a subscription summary, and a Zambia phone field with a `+260` placeholder. With no payment provider configured, the phone field and checkout are disabled, the interface explains that no payment or activation occurred, and choosing a plan does not send a guest to sign-in. PawaPay is unavailable and the native Google Play billing bridge is absent.

**Status: PARTIALLY FUNCTIONAL.** Plan discovery and the no-provider state are implemented and tested. Real registration, payment instructions tied to a live provider, payment-pending/confirmation screens, active status/renewal, and manage/cancel flows cannot be verified or represented as complete without the provider and platform integration. No fake payment or activation is performed.

### Other verified behavior and issues fixed

- Free challenge submission is available to guests. The backend evaluates the submission without requiring an account, awards no guest XP, and saves no guest activity; the UI explains that guest results are not saved. A focused regression test covers this behavior.
- Subscription plan selection no longer redirects to sign-in when checkout is unavailable; the UI reports the provider limitation instead of implying a purchase.
- Direct Kite and Supabase password-recovery links now survive iframe initialization, open the new-password form, and clear the credential from the address bar; placeholder-token browser checks covered both formats.
- The existing Python logo is used as the favicon, and the application bundle URL is versioned to avoid stale cached JavaScript during updates.
- The 13 top-level routes, unknown-hash normalization, selected navigation systems, lesson/quiz/context flows, guest leaderboard periods, and responsive Settings layout were exercised in the browser as itemized above.
- The current unittest suite reports 81 tests: 73 passed and 8 Playwright browser tests were skipped because the Python Playwright package is not installed. Python compilation and `git diff --check` passed. The content-availability follow-up also passed eight focused regression tests and an isolated API/browser smoke check; this does not mean every control on every page was clicked.

### Remaining functional blockers and scope

| Feature | Status | Evidence / required next step |
|---|---|---|
| Browser/Playground and challenge code execution | **NOT FUNCTIONAL in this environment** | Browser submission reached the API but the isolated runner returned 503 because `runner:8010` could not resolve. Docker is unavailable. Runner-backed success paths are covered with test mocks; deploy and validate the isolated Linux/Docker runner before release. Do not replace it with unsafe in-process execution. |
| Live subscription checkout and lifecycle | **NOT IMPLEMENTED / unavailable** | Configure and activate a payment provider and any required native billing bridge, then verify registration, pending/confirmed payment, active entitlement, renewals, and cancellation end to end. |
| Authentication | **INTENTIONALLY DEFERRED (AUTHENTICATION)** | Authentication implementation and account-only APIs were not modified as part of this audit. Guest routes were exercised, but OAuth, real account journeys, and authenticated browser flows are not claimed as verified. |
| Unclicked individual controls and external-provider flows | **PARTIALLY VERIFIED** | See per-control results above. About/legal links, selected header/profile actions, signed-in navigation, and external/native payment flows were not all activated in a live provider environment. Complete these checks in a configured release environment. |
