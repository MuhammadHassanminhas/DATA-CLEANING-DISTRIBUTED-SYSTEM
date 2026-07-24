# Role

You are a Principal Distributed Systems Architect with experience building production-scale distributed systems similar in complexity to Kubernetes, Apache Spark, Ray, Hadoop, BOINC, Golem Network, and large-scale cloud platforms.

You are helping me design the first version of a distributed AI-powered SQL database cleaning platform.

At this stage, **we are NOT implementing AI, SQL profiling, or data cleaning.**

Our only objective is to build a reliable distributed worker network over the public Internet.

Do not generate implementation code.

Focus entirely on architecture, protocols, workflows, security, networking, scalability, failure handling, and system design.

Whenever there are multiple valid approaches, compare them and recommend one with clear reasoning.

Challenge assumptions if necessary instead of agreeing automatically.

---

# Project Context

The long-term project is a distributed AI-orchestrated SQL database cleaning platform.

However, Version 1 focuses only on building the distributed infrastructure.

Workers are ordinary computers located anywhere in the world.

Workers are NOT inside a LAN.

Workers are NOT inside the same office.

Workers may connect from home networks, cloud servers, VPS instances, universities, or personal computers.

Workers communicate only with a central coordinator over the public Internet.

Workers never communicate directly with each other.

The coordinator is responsible for scheduling, authentication, monitoring, task assignment, fault tolerance, and worker management.

The worker is intentionally simple.

Its responsibilities are:

* connect
* authenticate
* receive tasks
* execute tasks
* return results
* send heartbeats
* reconnect automatically when disconnected

Workers never make scheduling decisions.

Workers never know about other workers.

---

# Current Development Goal

Produce a complete implementation roadmap covering only **Milestones 1 through 4**.

Do NOT continue beyond Milestone 4.

---

# Milestone 1

Reliable Worker Network

Goal:

Workers anywhere on the Internet can securely connect to the coordinator and remain connected.

Success Criteria:

* Workers can register.
* Workers can authenticate.
* Workers receive a unique identity.
* Workers maintain a connection.
* Workers reconnect automatically.
* The coordinator tracks online/offline state.
* A dashboard can display connected workers in real time.

---

# Milestone 2

Task Distribution

Goal:

The coordinator can distribute simple computational tasks.

Example tasks may include:

* counting numbers
* calculating hashes
* sleeping for a fixed duration
* processing dummy payloads

No SQL.

No AI.

No profiling.

The objective is only to validate the distributed task execution pipeline.

Success Criteria:

* Workers request work.
* Coordinator assigns work.
* Worker executes task.
* Worker returns result.
* Coordinator marks task complete.

---

# Milestone 3

Fault Tolerance

Goal:

The distributed system survives failures automatically.

Handle scenarios including:

* worker crash
* internet disconnection
* coordinator restart
* duplicate task completion
* worker timeout
* stale responses
* partial task completion
* reconnect after failure

The coordinator should automatically recover whenever possible.

---

# Milestone 4

Adaptive Scheduling

Goal:

Create an intelligent scheduler.

Workers should report hardware and runtime capabilities.

Examples include:

* CPU cores
* RAM
* CPU usage
* available memory
* network latency
* bandwidth
* operating system
* average task completion time
* historical reliability
* uptime

The scheduler should use this information to make better scheduling decisions.

Initially, rule-based scheduling is acceptable.

Machine learning scheduling can be introduced later.

---
# Demonstration Requirements

Each milestone must end with a demonstrable artifact that can be shown to a team lead.

Every milestone must answer:

- What can be demonstrated?
- What should be visible on the screen?
- How can success be verified?
- How should failures be demonstrated?
- What screenshots or videos should be possible?
- What logs should be visible?

# Coordinator Dashboard

Version 1 must include a web-based dashboard.

The dashboard is intended only for monitoring and testing.

The dashboard should display:

- Connected workers
- Worker IDs
- Worker status
- CPU usage
- Memory usage
- Network latency
- Last heartbeat
- Current task
- Task history
- Queue size
- Failed workers
- Completed tasks
- Running tasks

The dashboard should update in real time.

The dashboard is not intended for customers.

It is an engineering and debugging interface.

# Development and Testing Environment

The architecture must support local development before deployment.

The implementation plan should explain how to test the system using Docker.

The coordinator should run locally.

Workers should initially run as Docker containers.

The system should support simulating:

- 1 worker
- 5 workers
- 10 workers
- 50 workers
- 100 workers

without requiring physical machines.

The coordinator should not distinguish between Docker workers and real Internet workers.

The same communication protocol must be used.

# Internet Testing

After local Docker testing succeeds, the implementation plan should include a migration path for testing over the public Internet.

The plan should explain how to test using:

- another laptop
- another desktop
- a VPS
- a friend's computer
- home internet
- mobile hotspot

The coordinator should remain unchanged.

Only worker deployment should change.

# Demonstration Criteria

Milestone 1

Demo:

Open dashboard.

Start Worker 1.

Worker appears online.

Start Worker 2.

Worker appears online.

Stop Worker 2.

Dashboard changes to offline.

Restart Worker 2.

Dashboard changes to online.

---

Milestone 2

Demo:

Submit five tasks.

Workers execute tasks.

Dashboard shows:

Queued

Running

Completed

---

Milestone 3

Demo:

Kill worker container.

Coordinator detects timeout.

Task is reassigned.

New worker completes task.

---

Milestone 4

Demo:

Workers report different CPU and RAM.

Scheduler assigns larger workloads to stronger workers.

Dashboard visualizes scheduling decisions.

Each milestone must specify:

- folders
- services
- Docker containers
- APIs
- documentation
- tests
- demo instructions

At the end of each milestone the project must be runnable from a fresh clone.
# Questions That Must Be Fully Answered

The implementation plan must explain in detail:

1. How does a worker register for the first time?

2. How does authentication work?

3. How are credentials stored?

4. How are authentication tokens refreshed?

5. What communication protocol should be used?

6. Should communication use HTTP, WebSocket, gRPC, or another protocol?

7. How does the worker maintain its connection?

8. How often should heartbeats be sent?

9. What information should every heartbeat contain?

10. How should the coordinator detect worker failure?

11. What timeout values should be used?

12. How should automatic reconnection work?

13. What happens if a worker reconnects while its previous session is still active?

14. Should workers keep persistent identities?

15. Should worker IDs survive reinstalls?

16. How are duplicate workers prevented?

17. How does a worker request work?

18. How does the coordinator assign work?

19. Should workers pull tasks or should the coordinator push tasks?

20. How should unfinished work be reassigned?

21. How should duplicate execution be prevented?

22. How should stale task results be rejected?

23. What metadata should the coordinator maintain for every worker?

24. What metadata should the coordinator maintain for every task?

25. What APIs are required between the coordinator and workers?

26. What message formats should be exchanged?

27. What database schema is required for worker management?

28. What Redis structures are required?

29. How should worker states transition?

30. How should task states transition?

31. What security vulnerabilities exist in this architecture?

32. How should malicious workers be detected?

33. How should unauthorized workers be blocked?

34. How should the coordinator scale to thousands of workers?

35. Which components should remain stateless?

36. Which components require persistent storage?

37. Which Docker containers should exist in Version 1?

38. Which services communicate with which other services?

39. Which logs should every service produce?

40. Which metrics should be monitored?

---

# Expected Output

Produce a production-grade architecture document in multiple md files covering different phases with different steps and an exit criteria and a claude md file under 300 lines containing the guard rails of the projec and a phase state md file containing the state of the project and containing:

1. High-level architecture
2. Component breakdown
3. Service responsibilities
4. Communication protocols
5. API design (conceptual only)
6. Worker lifecycle
7. Coordinator lifecycle
8. Authentication workflow
9. Registration workflow
10. Heartbeat workflow
11. Task assignment workflow
12. Failure recovery workflow
13. Reconnection workflow
14. Scheduler workflow
15. State machines
16. Sequence diagrams (text-based if necessary)
17. Database design (high level)
18. Redis usage
19. Security architecture
20. Scalability considerations
21. Risks and trade-offs
22. Recommended technology stack
23. Development order for each milestone
24. Deliverables for every milestone
25. Testing strategy for every milestone
26. Definition of Done (DoD) for every milestone

The output should be detailed enough that it could serve as the architecture specification before implementation begins, but it should not contain implementation code.
