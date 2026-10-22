# Simulation models for Verilator

Verilator compiles no VHDL. This folder holds Verilog for the VHDL entities of the Oasis vFPGA, for `sim/vl/build.sh` of the Coyote submodule (`VL_MODELS=hardware/sim_models`):

- `MehdiFIFO.sv`: hand-written SystemVerilog with the generics, ports and cycle behaviour of `parcore/libstf/hardware/src/hdl/fifo/fifo.vhd`. Hand-written because GHDL elaborates one entity with fixed generics, and the vFPGA instantiates the FIFO with three `DEPTH`/`WIDTH` pairs. Moves to libstf when upstream takes it.
- `gen_vhsnunzip.sh`: converts `parcore/vhsnunzip` (the Snappy decompressor, 12 VHDL files, top `vhsnunzip_quad`) to `vhsnunzip_quad.v` with GHDL (`--synth --out=verilog`, VHDL-2008 with `-frelaxed -fsynopsys`; the `.sim.vhd` variants, no Xilinx primitives). Needs GHDL 5 or newer, for example from the YosysHQ OSS CAD Suite. The output is 11 MB and not committed; run the script after a checkout and after parcore moves to a new vhsnunzip.

xsim and synthesis keep using the VHDL. Both models carry `timescale 1ns / 1ps` like the Oasis sources. Checked 2026-10-22: TPC-H Q6 (`scenarios/tpch_q6`) gives the same results under Verilator as in xsim; the decoder counters differ by a few cycles (`sim/README.md` of the submodule, "Running the simulation with Verilator").
