// Simulation model of libstf's MehdiFIFO (parcore/libstf/hardware/src/hdl/fifo/fifo.vhd) for Verilator,
// which compiles no VHDL. Same generics, ports and cycle behaviour as the VHDL entity:
// a DEPTH-entry FIFO with a registered output and a one-cycle bubble after a write into an empty FIFO.
// STYLE (the ram_style attribute) has no effect in simulation.
// Not used by xsim or synthesis: both take the VHDL.

`timescale 1ns / 1ps

module MehdiFIFO #(
    parameter int DEPTH = 16,       // a power of two except 1
    parameter int WIDTH = 32,
    parameter string STYLE = "block"
) (
    input  logic             i_clk,
    input  logic             i_rst_n,

    input  logic [WIDTH-1:0] i_data,
    input  logic             i_valid,
    output logic             i_ready,

    output logic [WIDTH-1:0] o_data,
    output logic             o_valid,
    input  logic             o_ready,

    output logic [$clog2(DEPTH):0] o_filling_level
);
    localparam int ADDRESS_WIDTH = $clog2(DEPTH);

    logic [WIDTH-1:0] memory [DEPTH];

    logic write_en;
    logic [ADDRESS_WIDTH-1:0] write_ptr, next_write_ptr, write_ptr_succ;
    logic [ADDRESS_WIDTH-1:0] read_ptr, state_read_ptr, next_state_read_ptr, read_ptr_succ;
    logic full, next_full, empty, next_empty;
    logic [1:0] operation;
    logic write_after_empty = 1'b0, next_write_after_empty;
    logic [ADDRESS_WIDTH:0] filling_level, next_filling_level, filling_level_succ, filling_level_pred;

    // memory logic
    assign write_en = i_valid & ~full;

    always_ff @(posedge i_clk) begin
        if (write_en) begin
            memory[write_ptr] <= i_data;
        end
        o_data <= memory[read_ptr];
    end

    // next state registers
    always_ff @(posedge i_clk) begin
        if (!i_rst_n) begin
            state_read_ptr <= '0;
            write_ptr      <= '0;
            empty          <= 1'b1;
            full           <= 1'b0;
            filling_level  <= '0;
        end else begin
            state_read_ptr <= next_state_read_ptr;
            write_ptr      <= next_write_ptr;
            empty          <= next_empty;
            full           <= next_full;
            filling_level  <= next_filling_level;
        end
        write_after_empty <= next_write_after_empty;
    end

    // next state logic
    assign write_ptr_succ      = write_ptr + 1'b1;
    assign read_ptr_succ       = state_read_ptr + 1'b1;
    assign filling_level_succ  = filling_level + 1'b1;
    assign filling_level_pred  = filling_level - 1'b1;

    assign operation = {i_valid, o_ready};

    always_comb begin
        next_write_ptr         = write_ptr;
        next_full              = full;
        next_empty             = empty;
        next_write_after_empty = 1'b0;
        next_state_read_ptr    = state_read_ptr;
        read_ptr               = state_read_ptr;
        next_filling_level     = filling_level;

        case (operation)
            2'b00: begin
                // nothing to do
            end
            2'b01: begin
                // read
                if (!empty && !write_after_empty) begin
                    next_state_read_ptr = read_ptr_succ;
                    read_ptr            = read_ptr_succ;
                    next_full           = 1'b0;
                    if (write_ptr == read_ptr_succ) begin
                        next_empty = 1'b1;
                    end
                    next_filling_level = filling_level_pred;
                end
            end
            2'b10: begin
                // write
                if (!full) begin
                    next_write_ptr = write_ptr_succ;
                    if (empty) begin
                        next_write_after_empty = 1'b1;
                    end
                    next_empty = 1'b0;
                    if (filling_level == (ADDRESS_WIDTH + 1)'(DEPTH - 1)) begin
                        next_full = 1'b1;
                    end
                    next_filling_level = filling_level_succ;
                end
            end
            2'b11: begin
                // read and write
                if (empty) begin
                    next_write_ptr         = write_ptr_succ;
                    next_write_after_empty = 1'b1;
                    next_empty             = 1'b0;
                    next_filling_level     = filling_level_succ;
                end else if (full) begin
                    next_state_read_ptr = read_ptr_succ;
                    read_ptr            = read_ptr_succ;
                    next_full           = 1'b0;
                    next_filling_level  = filling_level_pred;
                end else begin
                    if (!write_after_empty) begin
                        next_state_read_ptr = read_ptr_succ;
                        read_ptr            = read_ptr_succ;
                        // one element in the FIFO and a read and a write at once
                        if (read_ptr_succ == write_ptr) begin
                            next_write_after_empty = 1'b1;
                        end
                    end else begin
                        next_filling_level = filling_level_succ;
                    end
                    next_write_ptr = write_ptr_succ;
                end
            end
            default: begin
                // unknown values
            end
        endcase
    end

    // output signals
    assign o_valid         = ~empty & ~write_after_empty;
    assign i_ready         = ~full & i_rst_n;
    assign o_filling_level = filling_level;

endmodule
