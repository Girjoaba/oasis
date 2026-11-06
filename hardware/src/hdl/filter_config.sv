`timescale 1ns / 1ps

import libstf::*;
import oasis::*;

`include "libstf_macros.svh"
`include "config_macros.svh"

/**
 * Queues one filter_conf_t per column chunk for the FilterStage of each decoder.
 *
 * Write registers per decoder: lo, hi, ctrl (the bits of filter_ctrl_t, bit 0 is emit) and lut_min.
 * The four writes form one entry, which the FilterStage takes for the next column chunk of the
 * decoder. The host writes exactly one entry per column chunk configuration, before it, in the same
 * order. A column chunk without filter gets {emit}.
 *
 * After the registers of the decoders, two write registers fill the lookup table of every decoder:
 * table_addr sets the word, table_data writes one word there and moves to the next word. The host
 * writes the region of a bitmap before the first entry that looks it up, and no entry in flight
 * may look up a word it writes.
 *
 * Read registers: the ID, the number of decoders, the FIFO depth, the rows of the selection bitmap
 * (FILTER_SEL_ROWS), the bits of the lookup table (FILTER_TABLE_BITS) and the features (bit
 * FILTER_FEATURE_BLOOM: the FilterLookup takes Bloom filters). The host needs no
 * handshake: the config writes take effect in order, so a region is in the tables before the
 * entries written after it.
 */
module FilterConfig #(
    parameter NUM_DECODERS
) (
    input logic clk,
    input logic rst_n,

    write_config_i.s write_config,
    read_config_i.s  read_config,

    ready_valid_i.m out[NUM_DECODERS], // #(filter_conf_t)

    output logic                table_write_valid,
    output filter_table_write_t table_write
);

localparam NUM_WRITE_REGS  = NUM_FILTER_CONFIG_WRITE_REGS;
localparam TABLE_ADDR_REG  = NUM_DECODERS * NUM_WRITE_REGS;
localparam TABLE_DATA_REG  = NUM_DECODERS * NUM_WRITE_REGS + 1;

`RESET_RESYNC // Reset pipelining

// -- Read -----------------------------------------------------------------------------------------
logic[AXIL_DATA_BITS - 1:0] values[NUM_FILTER_CONFIG_READ_REGS];

assign values[0] = FILTER_CONFIG_ID;
assign values[1] = NUM_DECODERS;
assign values[2] = MAX_NUM_ENQUEUED_FILTER_CONFS;
assign values[3] = FILTER_SEL_ROWS;
assign values[4] = FILTER_TABLE_BITS;
assign values[5] = AXIL_DATA_BITS'(FILTER_BLOOM && FILTER_TABLE_BITS > 0) << FILTER_FEATURE_BLOOM;

ConfigReadRegisterFile #(
    .NUM_REGS(NUM_FILTER_CONFIG_READ_REGS)
) inst_read_regs (
    .clk(clk),
    .rst_n(reset_synced),

    .in(read_config),
    .values(values)
);

// -- Write ----------------------------------------------------------------------------------------
// The entry is {ctrl, lo, hi, lut_min}, the order of the fields of filter_conf_t.
for (genvar I = 0; I < NUM_DECODERS; I++) begin
    ready_valid_i #(data64_t) lo(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 0, MAX_NUM_ENQUEUED_FILTER_CONFS, data64_t) inst_lo_reg (clk, reset_synced, write_config, lo);

    ready_valid_i #(data64_t) hi(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 1, MAX_NUM_ENQUEUED_FILTER_CONFS, data64_t) inst_hi_reg (clk, reset_synced, write_config, hi);

    ready_valid_i #(filter_ctrl_t) ctrl(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 2, MAX_NUM_ENQUEUED_FILTER_CONFS, filter_ctrl_t) inst_ctrl_reg (clk, reset_synced, write_config, ctrl);

    ready_valid_i #(data64_t) lut_min(clk, reset_synced);
    ConfigWriteFIFO #(I * NUM_WRITE_REGS + 3, MAX_NUM_ENQUEUED_FILTER_CONFS, data64_t) inst_lut_min_reg (clk, reset_synced, write_config, lut_min);

    ready_valid_i #(logic[2 * $bits(data64_t) - 1:0]) bounds(clk, reset_synced);
    ready_valid_i #(logic[3 * $bits(data64_t) - 1:0]) operands(clk, reset_synced);
    ReadyValidCombiner inst_bounds_combine (lo, hi, bounds);
    ReadyValidCombiner inst_operands_combine (bounds, lut_min, operands);
    ReadyValidCombiner inst_conf_combine (ctrl, operands, out[I]);
end

// -- Lookup table ---------------------------------------------------------------------------------
// One registered word write per table_data write, to the tables of all decoders.
logic[FILTER_TABLE_WORD_BITS - 1:0] table_addr;

always_ff @(posedge clk) begin
    if (reset_synced == 1'b0) begin
        table_addr          <= '0;
        table_write         <= 'X;
        table_write_valid   <= 1'b0;
    end else begin
        table_write_valid <= 1'b0;

        if (write_config.valid && write_config.addr == TABLE_ADDR_REG) begin
            table_addr <= write_config.data[FILTER_TABLE_WORD_BITS - 1:0];
        end
        if (write_config.valid && write_config.addr == TABLE_DATA_REG) begin
            table_addr          <= table_addr + 1;
            table_write.addr    <= table_addr;
            table_write.data    <= write_config.data;
            table_write_valid   <= 1'b1;
        end
    end
end

endmodule
