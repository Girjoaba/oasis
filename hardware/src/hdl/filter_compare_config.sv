`timescale 1ns / 1ps

import libstf::*;
import oasis::NUM_FILTER_COMPARE_CONFIG_REGS;
import oasis::FILTER_COMPARE_CONFIG_ID;
import oasis::filter_bounds_t;

`include "libstf_macros.svh"
`include "config_macros.svh"

module FilterCompareConfig #(
    parameter NUM_DECODERS
) (
    input logic clk,
    input logic rst_n,

    write_config_i.s write_config,
    read_config_i.s  read_config,

    output filter_bounds_t bounds[NUM_DECODERS]
);

localparam NUM_WRITE_REGS = NUM_FILTER_COMPARE_CONFIG_REGS;

`RESET_RESYNC // Reset pipelining

// -- Read -----------------------------------------------------------------------------------------
logic[AXIL_DATA_BITS - 1:0] values[2];
assign values[0] = FILTER_COMPARE_CONFIG_ID;
assign values[1] = NUM_DECODERS;

ConfigReadRegisterFile #(
    .NUM_REGS(2)
) inst_read_regs (
    .clk(clk),
    .rst_n(reset_synced),

    .in(read_config),
    .values(values)
);

// -- Write ----------------------------------------------------------------------------------------
// Per decoder: lo, hi and the control register {negate, enable}. The control register resets to
// disabled, so all values pass until the host writes it.
for (genvar I = 0; I < NUM_DECODERS; I++) begin
    data64_t lo, hi;
    logic    enable, negate;

    ConfigWriteRegister #(I * NUM_WRITE_REGS + 0, data64_t) inst_lo_reg (clk, write_config, lo);
    ConfigWriteRegister #(I * NUM_WRITE_REGS + 1, data64_t) inst_hi_reg (clk, write_config, hi);

    always_ff @(posedge clk) begin
        if (reset_synced == 1'b0) begin
            enable <= 1'b0;
            negate <= 1'b0;
        end else if (write_config.valid && write_config.addr == I * NUM_WRITE_REGS + 2) begin
            enable <= write_config.data[0];
            negate <= write_config.data[1];
        end
    end

    assign bounds[I] = {enable, negate, lo, hi};
end

endmodule
