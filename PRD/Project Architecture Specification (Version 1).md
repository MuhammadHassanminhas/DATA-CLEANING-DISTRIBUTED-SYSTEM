# **Project Architecture Specification (Version** 

# **1)** 

## **Project Name (Working Title)** 

#### **Distributed AI-Orchestrated SQL Database Cleaning Platform** 

# **Project Goal** 

The goal of this project is to design and eventually build a production-grade distributed platform capable of automatically profiling, understanding, planning, cleaning, validating, and repairing extremely large relational SQL databases by utilizing a decentralized network of internet-connected worker machines. 

This is **not** another ETL pipeline. 

This is **not** another SQL cleaning script. 

This is **not** another distributed computing framework. 

The objective is to build an intelligent distributed system where Artificial Intelligence is responsible for planning and orchestration while thousands of internet-connected workers execute the computationally expensive operations. 

The platform should eventually support databases ranging from a few gigabytes to many terabytes while remaining horizontally scalable. 

# **Scope of Version 1** 

To keep the architecture focused and achievable, Version 1 supports **only relational SQL databases** . 

Initially supported databases may include: 


- Microsoft SQL Server Managment System
 

 

Support for CSV, Excel, PDFs, images, NoSQL databases, OCR, and other data sources is explicitly outside the scope of Version 1 and should not influence architectural decisions. 

# **High-Level Vision** 

A customer wants to clean a large SQL database. 

Instead of running all computations on one expensive server, the platform distributes computational work across thousands of worker machines connected through the public Internet. 

Workers may exist anywhere in the world. 

Workers do not know each other. 

Workers never communicate directly. 

Every worker communicates only with the central coordinator. 

The coordinator is responsible for orchestration, scheduling, security, validation, and fault tolerance. 

# **Core Philosophy** 

Artificial Intelligence should **reason** , not perform heavy computation. 

Distributed workers should **compute** , not reason. 

The AI should never receive raw database contents. 

The AI should receive only compact metadata generated during distributed profiling. 

Heavy computation belongs to distributed workers. 

Decision making belongs to AI. 

# **Overall System Architecture** 

The platform consists of the following major services: 

1. Upload & Database Ingestion Service 

2. Distributed Profiling Engine 

3. Metadata Aggregation Service 

4. AI Planning Service 

5. Adaptive Distributed Scheduler 

6. Distributed Worker Network 

7. Execution Engine 

8. Verification Engine 

9. Result Assembly Service 

- 10.Reporting Service 

Each service should remain modular and independently evolvable. 

# **1. Upload & Database Ingestion Service** 

Responsibilities: 

- Accept customer uploads or database backups. 

- Restore the database into an isolated processing environment. 

- Verify integrity. 

- Encrypt stored data. 

- Register job metadata. 

- Extract database schema. 

This service is responsible only for ingestion. 

No cleaning occurs here. 

# **2. Distributed Profiling Engine** 

This stage performs no AI. 

The coordinator divides the database into logical partitions suitable for profiling. 

Distributed workers receive profiling tasks. 

Workers compute statistics including: 

- row counts 

- null counts 

- duplicate counts 

- primary keys 

- foreign keys 

- data types 

- value distributions 

- min/max 

- outliers 

- constraint violations 

- orphaned relationships 

- invalid formats 

- uniqueness 

- table sizes 

Workers return only profiling metadata. 

Workers never return entire tables. 

The coordinator merges all profiling metadata into a single database profile. 

# **3. Metadata Aggregation Service** 

Merge profiling results into a compact representation. 

The metadata should describe: 

- schema 

- table relationships 

- constraints 

- quality metrics 

- anomalies 

- distributions 

- statistics 

Regardless of database size, the metadata should remain compact enough for AI reasoning. 

# **4. AI Planning Service** 

The AI never reads the database. 

Instead, it receives the aggregated metadata profile. 

The AI acts as an experienced principal data engineer. 

Responsibilities include: 

- understanding the schema 

- identifying quality issues 

- selecting cleaning strategies 

- determining execution stages 

- classifying operations 

- generating an execution graph 

- recommending validation procedures 

The AI does **not** clean the data. 

The AI produces an execution plan. 

Example conceptual stages: 

Stage 1: 

Local normalization 

Stage 2: 

Relationship validation 

Stage 3: 

Duplicate detection 

Stage 4: 

Semantic corrections 

Stage 5: 

Final validation 

# **5. Adaptive Distributed Scheduler** 

The scheduler converts the execution graph into executable tasks. 

Responsibilities: 

- dynamic chunk generation 

- workload balancing 

- worker assignment 

- retry handling 

- fault recovery 

- chunk resizing 

- monitoring 

- scheduling optimization 

Workers continuously report: 

- CPU capacity 

- available RAM 

- network bandwidth 

- latency 

- historical processing speed 

- historical reliability 

- availability 

The scheduler dynamically adjusts chunk sizes according to worker performance. 

Future versions may replace rule-based scheduling with machine learning. 

# **6. Distributed Worker Network** 

Workers may exist anywhere on the Internet. 

Workers may belong to volunteers, organizations, cloud providers, or dedicated infrastructure. 

Workers communicate only with the coordinator. 

Workers never communicate directly with each other. 

Each worker follows a simple lifecycle: 

1. Request work. 

2. Receive a task. 

3. Process assigned data. 

4. Return results. 

5. Delete temporary state. 

6. Request another task. 

Workers remain stateless whenever possible. 

# **7. Execution Engine** 

The execution engine performs the work defined by the AI plan. 

Operations are divided into three categories. 

## **Local Operations** 

Examples: 

- trim whitespace 

- normalize dates 

- normalize phone numbers 

- standardize capitalization 

- remove invalid characters 

- normalize formatting 

These operations require only local data. 

## **Global Operations** 

Examples: 

- duplicate detection 

- cross-table validation 

- foreign key verification 

- referential integrity checks 

- entity grouping 

These operations require repartitioning and coordination. 

## **Semantic Operations** 

Examples: 

- correcting misspelled names 

- expanding abbreviations 

- standardizing medical terminology 

- resolving inconsistent textual values 

Only uncertain records should require AI-assisted reasoning. 

The entire database must never be sent to the AI. 

# **8. Verification Engine** 

The platform should never blindly trust worker outputs. 

Verification responsibilities include: 

- validating constraints 

- measuring quality improvements 

- detecting inconsistencies 

- consensus-based verification for important tasks 

- confidence scoring 

The verification engine ensures that the final database remains internally consistent. 

# **9. Result Assembly Service** 

Merge processed partitions into the final cleaned database. 

Maintain: 

- table integrity 

- relationships 

- ordering where required 

- metadata lineage 

The output should preserve the original database structure while improving data quality. 

# **10. Reporting Service** 

Generate a comprehensive report including: 

- quality score before and after cleaning 

- detected issues 

- corrected issues 

- duplicates removed 

- missing values corrected 

- execution timeline 

- AI decisions 

- worker statistics 

- processing performance 

- remaining warnings 

The customer should understand exactly what changed. 

# **Fault Tolerance** 

Workers may disconnect at any moment. 

The coordinator should: 

- detect failures 

- reassign unfinished work 

- prevent duplicate completion 

- maintain checkpoints 

- recover automatically 

The customer should never need to restart a job because of worker failures. 

# **Security Principles** 

Customer data is considered sensitive. 

The architecture should minimize unnecessary exposure. 

Workers should receive only the minimum amount of data required to complete their assigned task. 

All communication should be encrypted. 

Temporary worker data should be deleted after successful task completion. 

Future versions may explore stronger privacy-preserving execution techniques, but Version 1 focuses on building a secure and practical distributed architecture. 

# **Long-Term Goal** 

Although Version 1 focuses exclusively on relational SQL databases, the architecture should remain modular enough to support additional data sources in future releases. 

#### However, **all planning, discussions, and architectural decisions for this project should assume that Version 1 supports only SQL databases.** 

Every design decision should prioritize scalability, reliability, security, maintainability, and distributed execution over feature breadth. 



Customer 

│ 

▼ 

Upload SQL Database 

│ 

▼ 

Coordinator Server 

│ 

AI Planner + Scheduler 

│ 

┌────────────────────────┐┼ ▼ ▼ ▼ 

Internet Worker  Internet Worker  Internet Worker 

(Pakistan)      (Germany)         (Brazil) 



Execute       Execute       Execute 

└────────────────────────┘┼ 

▼ 

Verification + Merge 

▼ 

Clean SQL Database 



Customer │ ▼ 

1. Upload Gateway 

│ ▼ 

5. AI Planning Service │ ▼ 

6. Distributed Scheduler 

│ ▼ 

7. Worker Network 

│ ▼ 8. Verification Engine 

│ ▼ 

#### 9. Report Generator 

The AI is only one small part. 

The majority of the system is distributed systems engineering. 

# **Step 5 — Metadata Store** 

The coordinator merges all metadata. 

Instead of 

200 TB 

You now have 

10 MB 

Example 

{ 

"columns":53, "duplicates":53124, "nulls":0.12, "invalid_dates":421, "ocr_confidence":0.93 

} 

# **Step 6 — AI Planning** 

Now the LLM is finally called. 

Not on 

200 TB 

On 

10 MB 

Prompt 

You are a data engineering expert. 

Generate an execution graph. 

The output is NOT 

Clean data. 

Instead 

Stage1: normalize_phone Stage2: normalize_dates 

Stage3: repartition_by_patient 

Stage4: 

remove_duplicates 

Stage5: semantic_matching 

Stage6: validation 

Notice 

The LLM doesn't clean anything. 

It writes the workflow. 

# **Step 7 — Scheduler** 

This is probably the hardest service. 

It receives 

Execution Graph 

and 

10,000 Workers 

Each worker reports 

CPU 

RAM 

GPU 

Internet Speed 

Ping 

Previous Failures 

Availability 

The scheduler decides 

Worker A 

↓ 

500 MB 

Worker B 

↓ 

50 MB 

Worker C 

↓ 

- 2 GB 

No two workers necessarily get the same amount of work. 

# **Step 8 — Worker Engine** 

The worker software has several modules. 

#### Downloader 

↓ 

Executor 

↓ 

Profiler 

↓ 

Cleaner 

↓ 

Validator 

↓ 

#### Uploader 

Notice 

There is NO LLM here. 

Workers only execute instructions. 

That keeps costs low. 

# **Step 9 — Shuffle Engine** 

This is where Spark-like ideas come in. 

Suppose the execution graph says 

Find duplicates 

The scheduler says 

Shuffle by PatientID 

Now 

Patient 1 

always goes to 

Worker 8. 

Worker 8 now has ALL records for Patient 1, making duplicate detection possible without loading the entire dataset on one machine. 

# **Step 10 — Semantic AI Cleaning** 

Now only difficult cases are sent to AI. 

Example 

"M Hsn" 

↓ 

"Muhammad Hassan" 

or 

"Diabtes" 

↓ 

"Diabetes" 

Notice 

Not the entire dataset. 

Only uncertain rows. 

This reduces API cost dramatically. 

# **Step 11 — Verification** 

I think this is another place where you can innovate. 

Don't trust one worker. 

Suppose 

Worker A says 

Age 

↓ 

Delete 

Worker B 

↓ 

Keep 

Worker C 

↓ 

Correct to 42 

Now 

Consensus Engine 

chooses the best answer. 

Very similar to blockchain consensus. 

# **Step 12 — Merge** 

Now the coordinator rebuilds 

200 TB 

from 

#### 40,000 cleaned chunks 

# **Step 13 — Report** 

Customer receives 

Quality Score 

Before vs After 

Errors Found 

Duplicates Removed 

Missing Values Fixed 

AI Suggestions 

Processing Time 

Worker Statistics 

# **The architecture I'd build** 

Customer 

│ Upload Gateway │ Format Detection 

│ Internal Representation │ Distributed Profiling Workers 

│ Metadata Aggregation 

│ AI Planning Service 

│ 

Adaptive Distributed Scheduler │ ┌──────────┬──────────┬──────────┐ ▼ ▼ ▼ Worker A   Worker B   Worker C │ │ │ Execute    Execute    Execute └──────────┴──────────┘ │ Shuffle & Reduce │ AI Semantic Cleaning │ Verification Engine │ Merge Results │ Quality Report │ Download Output 

# **Here's where I think your project can become something I've never really seen** 

Most distributed systems are **execution engines** (Spark, Ray, Dask). They don't decide _what_ should happen; they execute a plan. 

Most LLM systems are **reasoning engines** . They don't execute distributed workloads. 

I think your opportunity is to combine those ideas into an **AI-Orchestrated Distributed Data Engineering Platform** . 

The LLM acts like a **chief data engineer** : 

- It analyzes the dataset profile. 

- It decides the processing strategy. 

- It classifies operations into local, global, and semantic tasks. 

- It generates an execution graph. 

- It adapts the workflow if validation finds problems. 

Meanwhile, the distributed engine handles the heavy lifting—profiling, partitioning, execution, retries, and verification. 

That separation keeps AI costs low while allowing the platform to scale to datasets far larger than any LLM could ever ingest directly. If you execute this well, you're not just building another data cleaner; you're building a distributed execution platform with AIdriven orchestration. That's a much more ambitious and technically distinctive project. 

Now we're getting into the part that companies like Google, Databricks, and Snowflake spend years optimizing. 

The problem you've identified is called a **global operation** . 

Some operations are **local** : 

Trim whitespace 

" John " 

↓ 

"John" 

A worker only needs one row. 

These are easy. 

But some operations are **global** . 

For example: 

Chunk A 

John Smith 03121234567 Chunk B 

John Smith 03121234567 

Neither worker knows the other exists. 

**The solution is NOT to give every worker the whole dataset** 

Many people think: 

Send entire dataset 

↓ 

Every worker 

That completely defeats the purpose of distributed computing. 

Instead, distributed systems use **multiple stages** . 

# **Stage 1 — Local Cleaning** 

Every worker performs operations that don't require knowledge of other rows. 

For example: 

- Trim whitespace 

- Normalize dates 

- Standardize phone numbers 

- Convert NULL values 

- Remove invalid characters 

- Fix capitalization 

Worker A 

Chunk A 

↓ 

Clean 

Worker B 

Chunk B 

↓ 

Clean 

# **Stage 2 — Global Analysis** 

Now every worker creates a **summary** , not the whole dataset. 

Example: 

John Smith 03121234567 

↓ 

Hash 

↓ 

9A7C4... 

Instead of sending millions of rows back, each worker sends fingerprints. 

# **Stage 3 — Shuffle** 

This is the brilliant idea behind Hadoop and Spark. 

Imagine: 

John 

↓ 

Hash 

↓ 

Bucket 17 

Another: 

John 

↓ 

Hash 

↓ 

Bucket 17 

Both records go to the same worker. 

Worker 8 now owns: 

John John John John 

Now it can detect duplicates. 

Notice: 

No worker has the whole dataset. 

Every worker has **only one bucket** . 

This process is called a **shuffle** . 

It is one of the most important ideas in distributed computing. 

# **Then Reduce** 

Worker 8 decides: 

John 

↓ 

Keep first 

↓ 

Remove others 

Only now can duplicates be removed correctly. 

# **Think of it like sorting mail** 

Imagine 100 million letters. 

You don't ask one post office to read all of them. 

Instead: 

Initial sorting 

Letters 

↓ 

City 

Then 

Lahore 

↓ 

One center 

Karachi 

↓ 

Another center 

Each center only processes its own city. 

The same idea applies to data. 

# **How Spark does it** 

Spark uses: 

Map 

↓ 

Shuffle 

↓ 

Reduce 

Map 

Clean locally 

Shuffle 

Group related records 

Reduce 

#### Perform global operation 

# **Your system could be even smarter** 

Here's where I think your project can become genuinely novel. 

Instead of treating all cleaning operations the same, classify them. 

## **Type A — Local** 

Examples: 

- Trim spaces 

- Fix dates 

- Normalize phone numbers 

- Correct capitalization 

- Remove invalid characters 

Run immediately on any worker. 

## **Type B — Partitioned** 

Examples: 

- Remove duplicates 

- Group by patient 

- Find repeated invoices 

These require the shuffle stage. 

## **Type C — AI** 

Examples: 

"M Hassan" 

↓ 

"Muhammad Hassan" 

or 

"Diabtes" 

↓ 

"Diabetes" 

These need semantic reasoning. 

Only those records get sent to the LLM (or another specialized model), not the whole dataset. 

# **This becomes an AI execution plan** 

Instead of the LLM saying: 

Clean this data. 

It outputs something like: 

Stage 1: 

- trim_whitespace 

- normalize_dates 

- normalize_phone 

Stage 2: 

- repartition_by_hash(patient_id) 

Stage 3: 

- remove_duplicates 

Stage 4: 

- semantic_name_matching 

Stage 5: - validation 

Notice what happened? 

The LLM isn't cleaning the data. 

It's generating an **execution graph** . 

Your distributed engine executes that graph. 

## **This is the architecture I would build if I were starting today** 

Upload Dataset │ ▼ Distributed Profiling │ ▼ AI Execution Planner (LLM) 

│ ┌─────────────┴─────────────┐ ▼ ▼ Local Operations          Global Operations (trim, normalize...)      (shuffle, dedupe...) │ │ └─────────────┬─────────────┘ ▼ AI Semantic Cleaning │ ▼ Validation Engine │ ▼ Cleaned Dataset + Report 

### **One suggestion that could make this research-level** 

I would **not** stop at "AI cleans data." 

I would make the AI decide **how to execute** the cleaning efficiently. 

For example, the planner could answer questions like: 

- Which operations are local? 

- Which require repartitioning? 

- Which columns should be hashed? 

- Which tasks should run on CPUs versus GPUs? 

- Which workers are best suited for each stage? 

- How should chunk sizes change based on observed worker performance? 

In other words, your novelty wouldn't just be distributed cleaning—it would be an **AIoptimized distributed execution engine** that plans and adapts the workflow automatically. That's a much stronger technical contribution than simply distributing rowby-row cleaning. 

The tech that i want to use now also include langsmith where needed as well in the tech stack

| Component                | Technology                | Why                             |
| ------------------------ | ------------------------- | ------------------------------- |
| Backend                  | FastAPI                   | Async, high performance         |
| AI Planner               | OpenAI API → Ollama later | Start simple, reduce cost later |
| Metadata DB              | PostgreSQL                | Reliable relational metadata    |
| Cache & Task Queue       | Redis                     | Fast scheduling and caching     |
| Object Storage           | MinIO                     | S3-compatible, free locally     |
| Containers               | Docker                    | Consistent deployments          |
| Local Orchestration      | Docker Compose            | Simple development              |
| Production Orchestration | Kubernetes (later)        | Horizontal scaling              |
| Reverse Proxy            | NGINX or Traefik          | TLS, routing, rate limiting     |
| Monitoring               | Prometheus + Grafana      | Metrics and dashboards          |
| Logs                     | Loki                      | Centralized logging             |
| CI/CD                    | GitHub Actions            | Automated testing and builds    |
| Infrastructure           | Terraform                 | Reproducible infrastructure     |
| Data Processing          | Polars + DuckDB + PyArrow | Fast analytical processing      |
| Testing                  | Pytest + Locust           | Correctness and load testing    |
| Package Manager          | uv                        | Fast dependency management      |
