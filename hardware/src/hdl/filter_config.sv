`timescale 1ns / 1ps

import libstf::*;
import oasis::*;

`include "libstf_macros.svh"
`include "config_macros.svh"

/**
 * Queues one filter_conf_t per column chunk for the FilterStage of each decoder.
 *
 * Write registers per decoder: lo, hi and ctrl (the bits of filter_ctrl_t, bit 0 is emit). The three
 * writes form one entry, which the FilterStage takes for the next column chunk of the decoder. The
 * host writes exactly one entry per column chunk configuration, before it, in the same order. A
 * column chunk without filter gets {emit}.
 *
 * Read registers: the ID, the number of decoders, the FIFO depth and the rows of the selection
 * bitmap (FILTER_SEL_ROWS).
 */
module FilterConfig #(
    parameter NUM_DECODERS
) (
    input logic clk,
    input logic rst_n,

    write_config_i.s write_config,
    read_config_i.s  read_config,

    ready_valid_i.m out[NUM_DECODERS] // #(filter_conf_t)
);

localparam NUM_WRITE_REGS = NUM_FILTER_CONFIG_WRITE_REGS;

`RESET_RESYNC // Reset pipelining

// -- Read -----------------------------------------------------------------------------------------
logic[AXIL_DATA_BITS - 1:0] values[NUM_FILTER_CONFIG_READ_REGS];
assign values[0] = FILTER_CONFIG_ID;
assign values[1] = NUM_DECODERS;
assign values[2] = MAX_NUM_ENQUEUED_FILTER_CONFS;
assign values[3] = FILTER_SEL_ROWS;

ConfigReadRegisterFile #(
    .NUM_REGS(NUM_FILTER_CONFIG_READ_REGS)
) inst_read_regs (
    .clk(clk),
    .rst_n(reset_synced),

    .in(read_config),
    .values(values)
);

// -- Write ----------------------------------------------------------------------------------------
// The entry is {ctrl, lo, hi}, the order of the fields of filter_conf_t.
for (genvar I = 0; I < NUM_DECODERS; I++) begin
    ready_valid_i #(data64_t) lo(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 0, MAX_NUM_ENQUEUED_FILTER_CONFS, data64_t) inst_lo_reg (clk, reset_synced, write_config, lo);

    ready_valid_i #(data64_t) hi(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 1, MAX_NUM_ENQUEUED_FILTER_CONFS, data64_t) inst_hi_reg (clk, reset_synced, write_config, hi);

    ready_valid_i #(filter_ctrl_t) ctrl(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 2, MAX_NUM_ENQUEUED_FILTER_CONFS, filter_ctrl_t) inst_ctrl_reg (clk, reset_synced, write_config, ctrl);

    ready_valid_i #(logic[2 * $bits(data64_t) - 1:0]) bounds(clk, reset_synced);
    ReadyValidCombiner inst_bounds_combine (lo, hi, bounds);
    ReadyValidCombiner inst_conf_combine (ctrl, bounds, out[I]);
end

endmodule
