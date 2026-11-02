package oasis;

import libstf::vaddress_t;
import libstf::size_t;
import libstf::data32_t;
import libstf::data64_t;

parameter longint unsigned OASIS_SYSTEM_ID = 64'h0A515;

parameter int NUM_READ_REQ_CONFIG_REGS = 3;
parameter longint unsigned READ_REQ_CONFIG_ID = 64'h2f966a70f04c0e93;

typedef struct packed {
    vaddress_t vaddr;
    size_t     len;
} read_req_t;

// -- FilterStage ----------------------------------------------------------------------------------
// Rows of the selection bitmap of one FilterStage: the rows of one row group whose selection the
// stage carries from its filter columns to its other columns (16 KiB of BRAM per decoder).
parameter int FILTER_SEL_ROWS = 2 ** 17;

// Per decoder three write registers (lo, hi, ctrl) and four read registers (ID, number of
// decoders, FIFO depth, FILTER_SEL_ROWS). The GlobalConfig sizes both sides with one value.
parameter int NUM_FILTER_CONFIG_WRITE_REGS = 3;
parameter int NUM_FILTER_CONFIG_READ_REGS  = 4;
parameter int MAX_NUM_ENQUEUED_FILTER_CONFS = 64;
parameter longint unsigned FILTER_CONFIG_ID = 64'h14e8418192d46a72;

function automatic int FILTER_CONFIG_REGS(input int num_decoders);
    return NUM_FILTER_CONFIG_WRITE_REGS * num_decoders > NUM_FILTER_CONFIG_READ_REGS ?
           NUM_FILTER_CONFIG_WRITE_REGS * num_decoders : NUM_FILTER_CONFIG_READ_REGS;
endfunction

// What the FilterStage does with one column chunk. The bits are the ctrl register of FilterConfig.
//   compare:   a value passes if lo <= x <= hi (negate: if not). Without compare every value passes.
//   load_sel:  AND the selection bitmap of the decoder into the pass bits (the previous filter
//              columns of the row group).
//   store_sel: write the pass bits into the selection bitmap.
//   emit:      write the passing values to the host. Without emit the output of the chunk is empty.
typedef struct packed {
    logic compare;
    logic negate;
    logic load_sel;
    logic store_sel;
    logic emit;
} filter_ctrl_t;

typedef struct packed {
    filter_ctrl_t ctrl;
    data64_t      lo;
    data64_t      hi;
} filter_conf_t;

// One 32 bit lane of the FilterCompact with the keep bits of its bytes, so that a partial last
// lane of a BYTE or INT16 column survives the compaction unchanged.
typedef struct packed {
    data32_t   data;
    logic[3:0] keep;
} filter_lane_t;

endpackage
