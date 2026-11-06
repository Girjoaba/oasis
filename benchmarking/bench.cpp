// Throughput and per-query latency of DuckDB on a query suite (TPC-H, ClickBench), scanning through
// Oasis (read_oasis) or through the DuckDB Parquet reader (read_parquet). One process holds all
// sessions: the Oasis context takes every free hugepage, so the query streams of the throughput test
// are connections of one database. bench.sh starts one process per cell (suite, mode, thread count,
// repetition), pinned to the first n cores.
//
// The suite folder (--suite) holds tables.txt (the names of the tables, one view each) and the queries
// as qNN.sql; the number of a query is the number in its file name.
//
// Modes (the source of the views):
//   oasis        read_oasis('rdma://<t>.parquet'), oasis_hw_filter = true, every stage (the FilterStage)
//   oasis_s<k>   the same with oasis_hw_filter_stage = k (0 to 5): the stages up to k only
//   oasis_nohw   read_oasis('rdma://<t>.parquet'), oasis_hw_filter = false (filters on the CPU)
//   native_rdma  read_parquet('rdma://<t>.parquet'): DuckDB decodes, the bytes come over the bypass stream
//   native_local read_parquet('<local-dir>/<t>.parquet')
//
// Phases, in this order:
//   warmup       SELECT * FROM <t> LIMIT 1 for every table: fills the Parquet metadata cache
//   throughput   --tp-reps runs of --sessions streams, each the queries in a random order (seed + stream);
//                without a latency pass, the rows of stream 0 of the first run go to results/
//   latency      --lat-reps passes over the queries on one connection, with the decoder counters and the
//                DuckDB JSON profile of every query; the rows of the first pass go to results/
//
// Output in --out: meta.json, settings.tsv, warmup.jsonl, throughput.jsonl, latency.jsonl,
// results/qNN.tsv, profile/qNN_rR.json. Every line is flushed when written, so a killed run keeps
// what it measured.

#include "duckdb.hpp"

#include <sys/resource.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using namespace duckdb;
using Clock = std::chrono::steady_clock;

namespace {

struct Options {
    std::string mode = "oasis";
    int         stage = 5;
    std::string server;
    uint64_t    port = 0;
    std::string local_dir;
    int         threads = 8;
    int         sessions = 4;
    int         tp_reps = 1;
    int         lat_reps = 0;
    bool        profile = true;
    uint64_t    seed = 42;
    int         prefetch = 2;
    std::string memory_limit;
    std::string suite;
    std::string queries;
    std::string out = ".";
};

void usage() {
    std::cerr << "usage: bench --mode oasis|oasis_s<k>|oasis_nohw|native_rdma|native_local --suite DIR --out DIR\n"
                 "                  [--server IP] [--port N] [--local-dir DIR] [--threads N] [--sessions N]\n"
                 "                  [--tp-reps N] [--lat-reps N] [--no-profile] [--seed N] [--prefetch N]\n"
                 "                  [--memory-limit SIZE] [--queries 1,6,14]\n";
}

Options parse(int argc, char **argv) {
    Options o;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                throw std::runtime_error("missing value of " + a);
            }
            return argv[++i];
        };
        if (a == "--mode") o.mode = next();
        else if (a == "--server") o.server = next();
        else if (a == "--port") o.port = std::stoull(next());
        else if (a == "--local-dir") o.local_dir = next();
        else if (a == "--threads") o.threads = std::stoi(next());
        else if (a == "--sessions") o.sessions = std::stoi(next());
        else if (a == "--tp-reps") o.tp_reps = std::stoi(next());
        else if (a == "--lat-reps") o.lat_reps = std::stoi(next());
        else if (a == "--no-profile") o.profile = false;
        else if (a == "--seed") o.seed = std::stoull(next());
        else if (a == "--prefetch") o.prefetch = std::stoi(next());
        else if (a == "--memory-limit") o.memory_limit = next();
        else if (a == "--suite") o.suite = next();
        else if (a == "--queries") o.queries = next();
        else if (a == "--out") o.out = next();
        else if (a == "-h" || a == "--help") { usage(); std::exit(0); }
        else throw std::runtime_error("unknown argument " + a);
    }
    if (o.mode.size() == 8 && o.mode.compare(0, 7, "oasis_s") == 0 && o.mode[7] >= '0' && o.mode[7] <= '5') {
        o.stage = o.mode[7] - '0';
    } else if (o.mode != "oasis" && o.mode != "oasis_nohw" && o.mode != "native_rdma" &&
               o.mode != "native_local") {
        throw std::runtime_error("unknown mode " + o.mode);
    }
    if (o.mode != "native_local" && o.server.empty()) {
        throw std::runtime_error("--server is required for mode " + o.mode);
    }
    if (o.mode == "native_local" && o.local_dir.empty()) {
        throw std::runtime_error("--local-dir is required for mode native_local");
    }
    if (o.suite.empty()) {
        throw std::runtime_error("--suite is required");
    }
    return o;
}

bool is_oasis(const Options &o) {
    return o.mode.compare(0, 5, "oasis") == 0;
}

std::string json_str(const std::string &s) {
    std::string r = "\"";
    for (unsigned char c : s) {
        switch (c) {
        case '"': r += "\\\""; break;
        case '\\': r += "\\\\"; break;
        case '\n': r += "\\n"; break;
        case '\r': r += "\\r"; break;
        case '\t': r += "\\t"; break;
        default:
            if (c < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                r += buf;
            } else {
                r += static_cast<char>(c);
            }
        }
    }
    return r + "\"";
}

// A value as a JSON number when it is a finite one, else as a string
std::string json_num(const std::string &s) {
    if (s == "NULL") {
        return "null";
    }
    char *end = nullptr;
    double d = std::strtod(s.c_str(), &end);
    if (s.empty() || *end != '\0' || d != d || d - d != 0) {
        return json_str(s);
    }
    return s;
}

double seconds_since(Clock::time_point t0) {
    return std::chrono::duration<double>(Clock::now() - t0).count();
}

struct CpuTime {
    double user = 0;
    double sys = 0;
};

CpuTime cpu_now() {
    rusage ru {};
    getrusage(RUSAGE_SELF, &ru);
    return {ru.ru_utime.tv_sec + ru.ru_utime.tv_usec * 1e-6, ru.ru_stime.tv_sec + ru.ru_stime.tv_usec * 1e-6};
}

// Rows of a result as a vector of (column name, value) lists
using Row = std::vector<std::pair<std::string, std::string>>;

std::vector<Row> rows_of(QueryResult &r) {
    std::vector<Row> rows;
    std::vector<std::string> names;
    for (auto &n : r.GetNames()) {
        names.push_back(n.GetIdentifierName());
    }
    while (auto chunk = r.Fetch()) {
        for (idx_t i = 0; i < chunk->size(); i++) {
            Row row;
            for (idx_t c = 0; c < chunk->ColumnCount(); c++) {
                auto v = chunk->GetValue(c, i);
                row.emplace_back(names[c], v.IsNull() ? "NULL" : v.ToString());
            }
            rows.push_back(std::move(row));
        }
    }
    return rows;
}

duckdb::unique_ptr<QueryResult> must(Connection &con, const std::string &sql) {
    auto r = con.Query(sql);
    if (r->HasError()) {
        throw std::runtime_error(sql + ": " + r->GetError());
    }
    return r;
}

// The counters of every stream slot as a JSON array. Reading the last profile register stops the
// profilers and holds their counters until the next data beat zeroes them (stream_profiler.sv), so a
// read before and a read after a window give the cycles of that window.
std::string read_profile(Connection &ctl) {
    auto r = ctl.Query("SELECT * FROM oasis_stream_profile()");
    if (r->HasError()) {
        return json_str("error: " + r->GetError());
    }
    std::string s = "[";
    bool first_row = true;
    for (auto &row : rows_of(*r)) {
        s += first_row ? "{" : ",{";
        first_row = false;
        bool first = true;
        for (auto &kv : row) {
            s += (first ? "" : ",") + json_str(kv.first) + ":" + json_num(kv.second);
            first = false;
        }
        s += "}";
    }
    return s + "]";
}

std::string source_of(const Options &o, const std::string &table) {
    if (is_oasis(o)) {
        return "read_oasis('rdma://" + table + ".parquet')";
    }
    if (o.mode == "native_rdma") {
        return "read_parquet('rdma://" + table + ".parquet')";
    }
    return "read_parquet('" + o.local_dir + "/" + table + ".parquet')";
}

std::string read_file(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    std::stringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

// The rows of a query result as results/qNN.tsv
void write_result(const std::string &dir, int nr, QueryResult &r, const std::vector<Row> &rows) {
    char rname[32];
    std::snprintf(rname, sizeof(rname), "q%02d.tsv", nr);
    std::ofstream f(dir + "/results/" + rname);
    for (size_t c = 0; c < r.ColumnCount(); c++) {
        f << (c ? "\t" : "") << r.GetNames()[c].GetIdentifierName();
    }
    f << "\n";
    for (auto &row : rows) {
        for (size_t c = 0; c < row.size(); c++) {
            f << (c ? "\t" : "") << row[c].second;
        }
        f << "\n";
    }
}

} // namespace

int main(int argc, char **argv) {
    Options o;
    try {
        o = parse(argc, argv);
    } catch (std::exception &e) {
        std::cerr << e.what() << "\n";
        usage();
        return 2;
    }
    namespace fs = std::filesystem;
    fs::create_directories(o.out + "/results");
    fs::create_directories(o.out + "/profile");
    auto t_start = Clock::now();

    try {
        // Loading the database opens the Oasis context (statically linked extension): hugepage pool,
        // system id check, TLB map. It happens for every mode.
        DuckDB db(nullptr);
        Connection ctl(db);
        std::cout << "database open: " << seconds_since(t_start) << " s" << std::endl;

        // GLOBAL, so the stream connections see the same settings
        must(ctl, "SET GLOBAL threads = " + std::to_string(o.threads));
        if (!o.memory_limit.empty()) {
            must(ctl, "SET GLOBAL memory_limit = '" + o.memory_limit + "'");
        }
        must(ctl, "SET GLOBAL parquet_metadata_cache = true");
        must(ctl, "SET GLOBAL enable_external_file_cache = false");
        must(ctl, "SET GLOBAL oasis_prefetch_depth = " + std::to_string(o.prefetch));
        must(ctl, "SET GLOBAL oasis_enable_yield = true");
        must(ctl, std::string("SET GLOBAL oasis_hw_filter = ") + (is_oasis(o) && o.mode != "oasis_nohw" ? "true" : "false"));
        if (is_oasis(o) && o.mode != "oasis_nohw") {
            must(ctl, "SET GLOBAL oasis_hw_filter_stage = " + std::to_string(o.stage));
        }
        if (!o.server.empty()) {
            must(ctl, "SET GLOBAL oasis_rdma_server = '" + o.server + "'");
        }
        if (o.port != 0) {
            must(ctl, "SET GLOBAL oasis_rdma_port = " + std::to_string(o.port));
        }
        std::vector<std::string> tables;
        {
            std::stringstream ss(read_file(o.suite + "/tables.txt"));
            for (std::string t; ss >> t;) {
                tables.push_back(t);
            }
        }
        if (tables.empty()) {
            throw std::runtime_error(o.suite + "/tables.txt is missing or empty");
        }
        for (auto &t : tables) {
            must(ctl, "CREATE VIEW " + t + " AS SELECT * FROM " + source_of(o, t));
        }

        // The queries as files qNN.sql (TPC-H: tpch_queries(); the tpch extension is not linked into libduckdb)
        std::vector<std::pair<int, std::string>> queries;
        for (auto &e : fs::directory_iterator(o.suite)) {
            auto name = e.path().filename().string();
            if (name.size() < 6 || name[0] != 'q' || e.path().extension() != ".sql" ||
                name.find_first_not_of("0123456789", 1) != name.size() - 4) {
                continue;
            }
            std::string sql = read_file(e.path().string());
            if (sql.empty()) {
                throw std::runtime_error(e.path().string() + " is empty");
            }
            queries.emplace_back(std::stoi(name.substr(1)), sql);
        }
        std::sort(queries.begin(), queries.end());
        if (queries.empty()) {
            throw std::runtime_error("no qNN.sql in " + o.suite);
        }
        if (!o.queries.empty()) {
            std::vector<int> keep;
            std::stringstream ss(o.queries);
            for (std::string q; std::getline(ss, q, ',');) {
                keep.push_back(std::stoi(q));
            }
            queries.erase(std::remove_if(queries.begin(), queries.end(),
                                         [&](auto &q) {
                                             return std::find(keep.begin(), keep.end(), q.first) == keep.end();
                                         }),
                          queries.end());
        }

        // meta.json and every setting
        {
            std::ofstream m(o.out + "/meta.json");
            m << "{" << json_str("mode") << ":" << json_str(o.mode) << "," << json_str("suite") << ":"
              << json_str(fs::path(o.suite).filename().string()) << "," << json_str("hw_filter_stage") << ":"
              << (is_oasis(o) && o.mode != "oasis_nohw" ? o.stage : 0) << "," << json_str("server") << ":"
              << json_str(o.server) << "," << json_str("port") << ":" << o.port << "," << json_str("local_dir") << ":"
              << json_str(o.local_dir) << "," << json_str("threads") << ":" << o.threads << ","
              << json_str("sessions") << ":" << o.sessions << "," << json_str("tp_reps") << ":" << o.tp_reps << ","
              << json_str("lat_reps") << ":" << o.lat_reps << "," << json_str("profile") << ":"
              << (o.profile ? "true" : "false") << "," << json_str("seed") << ":" << o.seed << ","
              << json_str("prefetch") << ":" << o.prefetch << "," << json_str("memory_limit") << ":"
              << json_str(o.memory_limit) << "," << json_str("queries") << ":" << queries.size();
            auto v = ctl.Query("SELECT * FROM pragma_version()");
            if (!v->HasError()) {
                for (auto &row : rows_of(*v)) {
                    for (auto &kv : row) {
                        m << "," << json_str("duckdb_" + kv.first) << ":" << json_str(kv.second);
                    }
                }
            }
            m << "}\n";
            std::ofstream st(o.out + "/settings.tsv");
            auto s = must(ctl, "SELECT name, value FROM duckdb_settings() ORDER BY name");
            for (auto &row : rows_of(*s)) {
                st << row[0].second << "\t" << row[1].second << "\n";
            }
        }

        // Warm-up: the metadata of every file (fills the Parquet metadata cache)
        {
            std::ofstream w(o.out + "/warmup.jsonl");
            for (auto &t : tables) {
                auto t0 = Clock::now();
                must(ctl, "SELECT * FROM " + t + " LIMIT 1");
                w << "{" << json_str("table") << ":" << json_str(t) << "," << json_str("seconds") << ":"
                  << seconds_since(t0) << "}" << std::endl;
            }
        }
        std::cout << "warm-up done: " << seconds_since(t_start) << " s" << std::endl;

        // Throughput: --sessions streams, each the queries in its own random order
        std::ofstream tp(o.out + "/throughput.jsonl");
        for (int rep = 0; rep < o.tp_reps; rep++) {
            std::string prof_before = read_profile(ctl);
            std::vector<std::string> records(o.sessions);
            std::atomic<int> errors {0};
            auto cpu0 = cpu_now();
            auto t0 = Clock::now();
            std::vector<std::thread> workers;
            for (int s = 0; s < o.sessions; s++) {
                workers.emplace_back([&, s] {
                    Connection con(db);
                    auto order = queries;
                    std::shuffle(order.begin(), order.end(), std::mt19937(o.seed + s));
                    std::string rec;
                    for (size_t pos = 0; pos < order.size(); pos++) {
                        double start = seconds_since(t0);
                        double end = -1;
                        std::string error;
                        try {
                            auto r = con.Query(order[pos].second);
                            end = seconds_since(t0);
                            if (r->HasError()) {
                                error = r->GetError();
                            } else if (rep == 0 && s == 0 && o.lat_reps == 0) {
                                // After the end of the query, but inside the time of the run (results are small)
                                auto rows = rows_of(*r);
                                write_result(o.out, order[pos].first, *r, rows);
                            }
                        } catch (std::exception &e) {
                            error = std::string("exception: ") + e.what();
                        }
                        if (end < 0) {
                            end = seconds_since(t0);
                        }
                        bool ok = error.empty();
                        if (!ok) {
                            errors++;
                        }
                        rec += std::string(rec.empty() ? "" : ",") + "{" + json_str("stream") + ":" + std::to_string(s) +
                               "," + json_str("pos") + ":" + std::to_string(pos) + "," + json_str("q") + ":" +
                               std::to_string(order[pos].first) + "," + json_str("start") + ":" +
                               std::to_string(start) + "," + json_str("end") + ":" + std::to_string(end) + "," +
                               json_str("ok") + ":" + (ok ? "true" : "false") +
                               (ok ? "" : "," + json_str("error") + ":" + json_str(error)) + "}";
                    }
                    records[s] = rec;
                });
            }
            for (auto &w : workers) {
                w.join();
            }
            double sec = seconds_since(t0);
            auto cpu1 = cpu_now();
            std::string prof = read_profile(ctl);
            double n = static_cast<double>(o.sessions) * queries.size();
            tp << "{" << json_str("rep") << ":" << rep << "," << json_str("threads") << ":" << o.threads << ","
               << json_str("sessions") << ":" << o.sessions << "," << json_str("queries") << ":" << n << ","
               << json_str("seconds") << ":" << sec << "," << json_str("qph") << ":" << n * 3600.0 / sec << ","
               << json_str("cpu_user_s") << ":" << cpu1.user - cpu0.user << "," << json_str("cpu_sys_s") << ":"
               << cpu1.sys - cpu0.sys << "," << json_str("errors") << ":" << errors.load() << ","
               << json_str("profile") << ":" << prof << "," << json_str("profile_before") << ":" << prof_before
               << "," << json_str("runs") << ":[";
            bool first = true;
            for (auto &r : records) {
                if (!r.empty()) {
                    tp << (first ? "" : ",") << r;
                    first = false;
                }
            }
            tp << "]}" << std::endl;
            std::printf("throughput rep %d: %.1f s, %.0f queries per hour, cpu %.0f s user %.0f s sys, %d errors\n",
                        rep, sec, n * 3600.0 / sec, cpu1.user - cpu0.user, cpu1.sys - cpu0.sys, errors.load());
            std::fflush(stdout);
        }

        // Latency: one connection, the queries in order, the counters and the profile of each
        if (o.lat_reps > 0) {
            std::ofstream lat(o.out + "/latency.jsonl");
            Connection con(db);
            std::string last_profile = o.out + "/profile/last.json";
            bool profiling = false;
            if (o.profile) {
                auto a = con.Query("SET enable_profiling = 'json'");
                auto b = con.Query("SET profiling_output = '" + last_profile + "'");
                profiling = !a->HasError() && !b->HasError();
                if (!profiling) {
                    std::cerr << "WARNING: no DuckDB profile: " << (a->HasError() ? a->GetError() : b->GetError())
                              << std::endl;
                }
            }
            for (int rep = 0; rep < o.lat_reps; rep++) {
                for (auto &q : queries) {
                    read_profile(ctl);
                    std::remove(last_profile.c_str());
                    auto cpu0 = cpu_now();
                    auto t0 = Clock::now();
                    auto r = con.Query(q.second);
                    double sec = seconds_since(t0);
                    auto cpu1 = cpu_now();
                    char name[32];
                    std::snprintf(name, sizeof(name), "q%02d_r%d.json", q.first, rep);
                    // Copied before the next statement of this connection overwrites it
                    if (profiling) {
                        std::ofstream(o.out + "/profile/" + name) << read_file(last_profile);
                    }
                    std::string prof = read_profile(ctl);
                    bool ok = !r->HasError();
                    size_t nrows = 0;
                    if (ok) {
                        auto rows = rows_of(*r);
                        nrows = rows.size();
                        if (rep == 0) {
                            write_result(o.out, q.first, *r, rows);
                        }
                    }
                    lat << "{" << json_str("rep") << ":" << rep << "," << json_str("q") << ":" << q.first << ","
                        << json_str("seconds") << ":" << sec << "," << json_str("cpu_user_s") << ":"
                        << cpu1.user - cpu0.user << "," << json_str("cpu_sys_s") << ":" << cpu1.sys - cpu0.sys << ","
                        << json_str("rows") << ":" << nrows << "," << json_str("ok") << ":" << (ok ? "true" : "false")
                        << (ok ? "" : "," + json_str("error") + ":" + json_str(r->GetError())) << ","
                        << json_str("profile") << ":" << prof << "}" << std::endl;
                    std::printf("latency rep %d q%02d: %.3f s%s\n", rep, q.first, sec, ok ? "" : " ERROR");
                    std::fflush(stdout);
                }
            }
            std::remove(last_profile.c_str());
        }
    } catch (std::exception &e) {
        std::cerr << "FAILED: " << e.what() << std::endl;
        return 1;
    }
    std::cout << "done: " << seconds_since(t_start) << " s" << std::endl;
    return 0;
}
