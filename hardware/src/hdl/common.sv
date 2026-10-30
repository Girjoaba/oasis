package oasis;

import libstf::vaddress_t;
import libstf::size_t;
import libstf::data64_t;

parameter longint unsigned OASIS_SYSTEM_ID = 64'h0A515;

parameter int NUM_READ_REQ_CONFIG_REGS = 3;
parameter longint unsigned READ_REQ_CONFIG_ID = 64'h2f966a70f04c0e93;

typedef struct packed {
    vaddress_t vaddr;
    size_t     len;
} read_req_t;

parameter int NUM_FILTER_COMPARE_CONFIG_REGS = 3;
parameter longint unsigned FILTER_COMPARE_CONFIG_ID = 64'h14e8418192d46a72;

// Closed interval lo <= x <= hi of one FilterCompare. INT32 columns use the low 32 bits of lo and hi.
// The values pass the interval or, with negate, its complement. Without enable all values pass.
typedef struct packed {
    logic    enable;
    logic    negate;
    data64_t lo;
    data64_t hi;
} filter_bounds_t;

endpackage
