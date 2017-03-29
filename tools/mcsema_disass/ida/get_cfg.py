#!/usr/bin/env python

##
## Instructions:
## 1) Install python-protobuf for your IDAPython installation. This probably means
## downloading it from https://protobuf.googlecode.com/files/protobuf-2.5.0.tar.gz
## and manually running setup.py
## 2) This script should be run via IDA's batch mode. See the output 
## of --help for more details on the command line options.
##

import idautils
import idaapi
import idc
import sys
from os import path
import os
import argparse
import struct
#import syslog
import traceback
import collections
import itertools
import util
from util import *

#hack for IDAPython to see google protobuf lib
if os.path.isdir('/usr/lib/python2.7/dist-packages'):
    sys.path.append('/usr/lib/python2.7/dist-packages')

if os.path.isdir('/usr/local/lib/python2.7/dist-packages'):
    sys.path.append('/usr/local/lib/python2.7/dist-packages')

tools_disass_ida_dir = os.path.dirname(__file__)
tools_disass_dir = os.path.dirname(tools_disass_ida_dir)

# Note: The bootstrap file will copy CFG_pb2.py into this dir!!
import CFG_pb2

_DEBUG = False
_DEBUG_FILE = sys.stderr

EXTERNALS = set()
DATA_SEGMENTS = {}

RECOVERED_EAS = set()
ACCESSED_VIA_JMP = set()

EMAP = {}
EMAP_DATA = {}

PIE_MODE = False

OFFSET_TABLES = {}

SPECIAL_REP_HANDLING = [ 
        [0xC3],
        ]

TRAPS = [ 
        idaapi.NN_int3,
        idaapi.NN_icebp,
        ]

CALLS = [
        idaapi.NN_call,
        idaapi.NN_callfi,
        idaapi.NN_callni]

RETS = [
        idaapi.NN_retf,
        idaapi.NN_retfd,
        idaapi.NN_retfq,
        idaapi.NN_retfw,
        idaapi.NN_retn,
        idaapi.NN_retnd,
        idaapi.NN_retnq,
        idaapi.NN_retnw]

COND_BRANCHES = [\
    idaapi.NN_ja,\
    idaapi.NN_jae,\
    idaapi.NN_jb,\
    idaapi.NN_jbe,\
    idaapi.NN_jc,\
    idaapi.NN_jcxz,\
    idaapi.NN_je,\
    idaapi.NN_jecxz,\
    idaapi.NN_jg,\
    idaapi.NN_jge,\
    idaapi.NN_jl,\
    idaapi.NN_jle,\
    idaapi.NN_jna,\
    idaapi.NN_jnae,\
    idaapi.NN_jnb,\
    idaapi.NN_jnbe,\
    idaapi.NN_jnc,\
    idaapi.NN_jne,\
    idaapi.NN_jng,\
    idaapi.NN_jnge,\
    idaapi.NN_jnl,\
    idaapi.NN_jnle,\
    idaapi.NN_jno,\
    idaapi.NN_jnp,\
    idaapi.NN_jns,\
    idaapi.NN_jnz,\
    idaapi.NN_jo,\
    idaapi.NN_jp,\
    idaapi.NN_jpe,\
    idaapi.NN_jpo,\
    idaapi.NN_jrcxz,\
    idaapi.NN_js,\
    idaapi.NN_jz,]

UCOND_BRANCHES = [\
    idaapi.NN_jmp,\
    idaapi.NN_jmpfi,\
    idaapi.NN_jmpni,\
    idaapi.NN_jmpshort]


EXTERNAL_NAMES = [
        "@@GLIBC_",\
        ]

EXTERNAL_DATA_COMMENTS = [
        "Copy of shared data",
        ]

def DEBUG(s):
    global _DEBUG, _DEBUG_FILE
    if _DEBUG:
        _DEBUG_FILE.write("{}\n".format(str(s)))
        _DEBUG_FILE.flush()

util.DEBUG = DEBUG

_PREFIX_ITYPES = (idaapi.NN_lock, idaapi.NN_rep,
                  idaapi.NN_repe, idaapi.NN_repne)


# Python 2.7's xrange doesn't work with `long`s.
def xrange(begin, end=None, step=1):
    if end:
        return iter(itertools.count(begin, step).next, end)
    else:
        return iter(itertools.count().next, begin)

def hasExternalDataComment(ea):
    cmt = idc.GetCommentEx(ea, 0)
    return cmt in EXTERNAL_DATA_COMMENTS

def readByte(ea):
    byte = readBytesSlowly(ea, ea+1)
    byte = ord(byte) 
    return byte

def readDword(ea):
    bytestr = readBytesSlowly(ea, ea+4)
    dword = struct.unpack("<L", bytestr)[0]
    return dword

def readQword(ea):
    bytestr = readBytesSlowly(ea, ea+8)
    qword = struct.unpack("<Q", bytestr)[0]
    return qword

def isElf():
    return idc.GetLongPrm(idc.INF_FILETYPE) == idc.FT_ELF

# Returns `True` if this is an ELF binary (as opposed to an ELF object file).
def isLinkedElf():
    return idc.GetLongPrm(idc.INF_FILETYPE) == idc.FT_ELF and \
        idc.BeginEA() not in [0xffffffffL, 0xffffffffffffffffL]

def IsString(ea):
    return idc.isASCII(idaapi.getFlags(ea))

def IsStruct(ea):
    return idc.isStruct(idaapi.getFlags(ea))

_FIXED_EXTERNAL_NAMES = {}

def fixExternalName(fn):
    if not fn:
        return ""

    orig_fn = fn
    if fn in _FIXED_EXTERNAL_NAMES:
        return _FIXED_EXTERNAL_NAMES[orig_fn]

    if fn in EMAP:
        return fn

    if fn in EMAP_DATA:
        return fn

    if not isLinkedElf() and fn[0] == '_':
        return fn[1:]

    if fn.endswith("_0"):
        newfn = fn[:-2]
        if newfn in EMAP:
            return newfn

    # found = False
    # for en in EXTERNAL_NAMES:
    #     if en in fn:
    #         found = True
    #         fn = fn[:fn.find(en)]
    #         break

    # In some cases we'll have something like a reference to `stderr_ptr` in
    # the `.got` section, and that will have a pointer that is the actual
    # external `stderr`.
    if isLinkedElf():
        ea = idc.LocByName(fn)
        if not is_code(ea) and ".got" in idc.SegName(ea).lower():
            for extref in idautils.DataRefsFrom(ea):
                name = get_symbol_name(extref, extref)
                if name:
                    fn = handleExternalRef(name) 

    _FIXED_EXTERNAL_NAMES[orig_fn] = fn
    return fn

def nameInMap(themap, fn):
    return fixExternalName(fn) in themap

def getFromEMAP(fname):
    fixname = fixExternalName(fname)
    return EMAP[fixname]

def isHlt(insn_t):
    return insn_t.itype == idaapi.NN_hlt

def isJmpTable(insn_t):
    is_jmp = insn_t.itype in [idaapi.NN_jmp, 
                              idaapi.NN_jmpfi,
                              idaapi.NN_jmpni]

    if not is_jmp:
        return False

    if idaapi.get_switch_info_ex(insn_t.ea):
        return True

    return False

def addFunction(M, ep):
    F = M.internal_funcs.add()
    F.entry_address = ep
    name = get_symbol_name(ep, ep)
    if name:
        F.symbol_name = name
    return F

def entryPointHandler(M, ep, name, args_from_stddef=False):

    EP = M.entries.add()
    EP.entry_name = name
    EP.entry_address = ep

    have_edata = False


    # should we get argument count  
    # calling ocnvention, and return type from std_defs?
    if args_from_stddef:
        try:
            (argc, conv, ret, sign) = getFromEMAP(name)
            have_edata = True
        except KeyError as ke:
            pass

    if not have_edata:
        (argc, conv, ret) = getExportType(name, ep)

    EP.entry_extra.entry_argc = argc
    EP.entry_extra.entry_cconv = conv
    if ret == 'Y':
        EP.entry_extra.does_return = False
    else:
        EP.entry_extra.does_return = True
    
    DEBUG("At EP {0}:{1:x}".format(name,ep))

def basicBlockHandler(M, F, block_ea, blockset, processed_blocks, new_func_eas):
    _, inst_eas, succ_eas = analyse_block(F.entry_address, block_ea)

    DEBUG("BB: {:x} in func {:x} with {} insts".format(
        block_ea, F.entry_address, len(inst_eas)))
    
    B = F.blocks.add()
    B.base_address = block_ea
    B.block_follows.extend(succ_eas)

    if _DEBUG:
        str_l = ["{0:x}".format(i) for i in succ_eas]
        if len(str_l) > 0:
            DEBUG("Successors: {}".format(", ".join(str_l)))

    for inst_ea in inst_eas:
        insn_t, inst_bytes = decode_instruction(inst_ea)
        I, endBlock = instructionHandler(
            M, B, insn_t, inst_bytes, inst_ea, new_func_eas)

        if is_direct_function_call(insn_t):
            called_ea = get_direct_branch_target(insn_t.ea)
            if is_noreturn_function(called_ea):
                I.local_noreturn = True

    return B
        
def isInternalCode(ea):

    pf = idc.GetFlags(ea)
    if idc.isCode(pf) and not idc.isData(pf):
        return True

    # find stray 0x90 (NOP) bytes in .text that IDA 
    # thinks are data items
    if readByte(ea) == 0x90:
        seg = idc.SegStart(ea)
        segtype = idc.GetSegmentAttr(seg, idc.SEGATTR_TYPE)
        if segtype == idc.SEG_CODE:
            mark_as_code(ea)
            return True

    return False

def isExternalSegment(ea):
    ext_types = []
    if ".got" in idc.SegName(ea).lower():
        return True

    seg = idc.SegStart(ea)
    if seg == idc.BADADDR:
        DEBUG("WARNING: Could not get segment addr for: {0:x}".format(ea))
        return False

    segtype = idc.GetSegmentAttr(seg, idc.SEGATTR_TYPE)
    if segtype == idc.SEG_XTRN:
        return True

    return False

def isExternalReference(ea):
    # see if this is in an internal or external code ref
    DEBUG("Testing {0:x} for externality".format(ea))
    if isExternalSegment(ea):
        return True

    if isLinkedElf():
        fn = getFunctionName(ea)
        for extsign in EXTERNAL_NAMES:
            if extsign in fn:
                DEBUG("Assuming external reference because: {} in {}".format(extsign, fn))
                return True

        if isExternalData(fn):
            if hasExternalDataComment(ea):
                return True
            else:
                DEBUG("WARNING: May have missed external data ref {} at {:x}".format(fn, ea))

    return False

def getFunctionName(ea):
    return idc.GetTrueNameEx(ea,ea)

_REFERENCE_OPERAND_TYPE = {
    Reference.IMMEDIATE: CFG_pb2.Reference.ImmediateOperand,
    Reference.DISPLACEMENT: CFG_pb2.Reference.MemoryDisplacementOperand,
    Reference.MEMORY: CFG_pb2.Reference.MemoryOperand,
    Reference.CODE: CFG_pb2.Reference.ControlFlowOperand,
}

def reference_target_type(ref):
    if is_code(ref.addr):
        return CFG_pb2.Reference.CodeTarget
    else:
        return CFG_pb2.Reference.DataTarget

def reference_operand_type(ref):
    global _REFERENCE_OPERAND_TYPE
    return _REFERENCE_OPERAND_TYPE[ref.type]

def reference_location(ref):
    if ref.symbol and isExternalReference(ref.addr):
        return CFG_pb2.Reference.External
    return CFG_pb2.Reference.Internal

MISSING_FUNCS = set()

def addInst(block, addr, insn_t, inst_bytes, new_func_eas):
    global EXTERNALS, PIE_MODE

    I = block.insts.add()
    I.inst_addr = addr  # May not be `insn_t.ea` because of prefix coalescing.
    I.inst_bytes = inst_bytes
    I.inst_len = len(inst_bytes)
    debug_info = ["  I: {:x}".format(addr)]
    refs = get_instruction_references(insn_t, PIE_MODE)

    for ref in refs:
        R = I.refs.add()
        R.target_type = reference_target_type(ref)
        R.operand_type = reference_operand_type(ref)
        R.location = reference_location(ref)
        R.address = ref.addr

        if ref.symbol:
            R.name = ref.symbol

            # Handle renaming things like `stderr_ptr` in the `.got` into
            # an external reference to `stderr`.
            if R.location == CFG_pb2.Reference.External:
                new_name = fixExternalName(ref.symbol)
                DEBUG("Fixing reference to {} into external reference to {}".format(
                    ref.symbol, new_name))
                ref.symbol = new_name
                R.name = ref.symbol
                EXTERNALS.add(ref.symbol)

        # Make sure anything that isn't yet resolved as a subroutine is resolved.
        if R.target_type == CFG_pb2.Reference.CodeTarget \
        and R.location == CFG_pb2.Reference.Internal \
        and not idc.GetFunctionName(ref.addr):
            DEBUG("Marking {:x} referenced from {:x} as a function".format(
                ref.addr, addr))
            try_mark_as_function(ref.addr)
            new_func_eas.add(ref.addr)

        # If this is a code ref, and if it's a target to a thunk, then we want
        # the lifter to treat the thunk as if it's the real function, so we
        # mark this code reference as being external.
        #
        # Note: If `isElfThunk` returns `True` then `name` is already in, or
        #       has been added, to the `EXTERNALS` set.
        is_thunk, name = isElfThunk(ref.addr)
        if is_thunk:
            DEBUG("Redirecting code ref from {:x} to thunk {:x} to external {}".format(
                addr, ref.addr, name))
            ref.symbol = name
            R.name = name
            R.location = CFG_pb2.Reference.External

        # If we changed the resolution of the thing, then go and update
        # the target and location types. Sometimes we'll get what looks like
        # a data reference, but ends up being resolved into a code reference
        # when we follow references through to their logical externals.
        new_addr = idc.LocByName(ref.symbol)
        if ref.addr != new_addr and idc.BADADDR != new_addr:
            DEBUG("Reference address changed from {:x} to {:x}".format(
                ref.addr, new_addr))
            ref.addr = new_addr
            R.target_type = reference_target_type(ref)
            R.location = reference_location(ref)

        debug_info.append(str(ref))

    DEBUG(" ".join(debug_info))

    return I

def isTrap(insn_t):
    return insn_t.itype in TRAPS

def handleExternalRef(fn):
    # Don't mangle symbols for fully linked ELFs... yet
    in_a_map = fn in EMAP or fn in EMAP_DATA
    if not isLinkedElf():
        if fn.startswith("__imp_"):
            fn = fn[6:]

        if fn.endswith("_0"):
            fn = fn[:-2]

        # name could have been modified by the above tests
        in_a_map = fn in EMAP or fn in EMAP_DATA

        if fn.startswith("_") and not in_a_map:
            fn = fn[1:]

        if fn.startswith("@") and not in_a_map:
            fn = fn[1:]

        if isElf() and '@' in fn:
            fn = fn[:fn.find('@')]

    fixfn = fixExternalName(fn)

    EXTERNALS.add(fixfn)
    return fixfn

def isInData(start_ea, end_ea):
    for (start,end) in DATA_SEGMENTS.values():
        if start_ea >= start and start_ea < end:
            DEBUG("Data Range: {0:x} <= {1:x} < {2:x}".format(start, start_ea, end))
            DEBUG("Data Range: {:x} - {:x}".format(start_ea, end_ea))
            if end_ea <= end:
                return True
            else:
                DEBUG("{0:x} NOT <= {1:x}".format(end_ea, end))
                DEBUG("{0:x}-{1:x} overlaps with: {2:x}-{3:x}".format(start_ea, end_ea, start, end))
                raise Exception("Overlapping data segments!")
        else:
            if end_ea > start and end_ea <= end:
                DEBUG("Overlaps with: {0:x}-{1:x}".format(start, end))
                raise Exception("Overlapping data segments!")

    return False

def isExternalData(fn):
    indata = fn in EMAP_DATA
    incode = fn in EMAP

    if indata and not incode:
        return True
    elif indata and incode:
        raise Exception("Symbol "+fn+" defined as both code and data!")
    else:
        return False


def sanityCheckJumpTableSize(table_ea, ecount):
    """ IDA doesn't correctly calculate some  jump table sizes. Fix them.

    This will look for the following jump tables:

    ----
    cmp eax, num_entries
    ja bad_entry
    fall_through:
    mov rax, qword [index * ptr_size + table_base_address]
    jmp rax
    bad_entry:
    ----


    IDA will detect these as jump tables, but it sometimes
    does not correctly calculate the "num_entries" properly,
    which leads us to missing jump table cases.

    Attempt to identify where 'num_entries' is compared, and
    sanity check it vs. what IDA found.

    """
    
    if not isLinkedElf():
        return ecount

    if getBitness() != 64:
        return ecount

    table_insn = idautils.DecodeInstruction(table_ea)
    if table_insn is None:
        DEBUG("Could not decode instruction at {:x}".format(table_insn))
        return ecount

    # This code is only reached if we *already know this is a jump table
    # The goal is to sanity check the size

    # First, Check to make sure that this is a "jmp reg" instruction. 
    if table_insn.Operands[0].type != idc.o_reg:
        return ecount

    DEBUG("Sanity checking table at {:x}".format(table_ea))

    # get register we jump with
    jmp_reg = table_insn.Operands[0].value

    inst_ea = table_ea
    # This will walk back up to 5 instructions looking for a 'cmp' against
    # the jump register, and use the immediate value from the cmp as 
    # the true jump table case count.

    # This strategy has the potential for false positives, since it
    # does not strictly check for the exact format of jump table instructions.
    # For now that is intentional to allow some flexibility, because we are
    # uncertain what the compiler will emit.

    #TODO(artem): Make this loop strict check for the instructions we expect,
    # if we find the current lax check causing false positives
    for i in xrange(5):
        # walk back a few instructions until we find a cmp
        inst_ea = idc.PrevHead(inst_ea)
        if inst_ea == idc.BADADDR:
            return ecount
        inst = idautils.DecodeInstruction(inst_ea)
        if inst is None:
            return ecount
        if inst.itype == idaapi.NN_cmp and inst.Operands[0].type == idc.o_reg:
            # check if reg in cmp == reg we jump with
            if jmp_reg == inst.Operands[0].value:
                # check if the CMP is with an immediate
                if inst.Operands[1].type == idc.o_imm:
                    # the immediate is our new count
                    # the comaprison is vs the max case#, but the cases start at 0, so add 1
                    # to get case count
                    new_count = 1 + inst.Operands[1].value
                    # compare to ecount. Take the bigger value.
                    if new_count > ecount:
                        DEBUG("Overriding old JMP count of {} with {} for table at {:x}".format(ecount, new_count, table_ea))
                        return new_count
            return ecount

    return ecount

def updateWithJmpTableTargets(inst, new_eas, new_func_eas):
    si = idaapi.get_switch_info_ex(inst)
    jsize = si.get_jtable_element_size()
    jstart = si.jumps
    # accept 32-bit jump tables in 64-bit, for now
    valid_sizes = [4, getBitness() / 8]
    readers = { 4: readDword,
                8: readQword }

    if jsize not in valid_sizes:
        raise Exception("Jump table is not a valid size: {}".format(jsize))
        return

    DEBUG("\tJMPTable Start: {0:x}".format(jstart))
    seg_start = idc.SegStart(jstart)

    if seg_start != idc.BADADDR:
        DEBUG("\tJMPTable offset from data: {:x}".format(jstart - seg_start))

    i = 0
    entries = si.get_jtable_size()
    entries = sanityCheckJumpTableSize(inst, entries)
    for i in xrange(entries):
        je = readers[jsize](jstart+i*jsize)
        # check if this is an offset based jump table
        if si.flags & idaapi.SWI_ELBASE == idaapi.SWI_ELBASE:
            # adjust jump target based on offset in table
            # we only ever see these as 32-bit offsets, even
            # when looking at 64-bit applications
            je = 0xFFFFFFFF & (je + si.elbase)

        if isStartOfFunction(je):
            DEBUG("\t\tJMPTable {0} references function: {1:x}".format(i, je))
            new_func_eas.add(je)
        else:
            DEBUG("\t\tAdding JMPTable {0}: {1:x}".format(i, je))
            new_eas.add(je)

def handleJmpTable(I, inst):
    si = idaapi.get_switch_info_ex(inst)
    jsize = si.get_jtable_element_size()
    jstart = si.jumps

    # accept 32-bit jump tables in 64-bit, for now
    valid_sizes = [4, getBitness()/8]
    readers = { 4: readDword,
                8: readQword }

    if jsize not in valid_sizes:
        raise Exception("Jump table is not a valid size: {}".format(jsize))
        return

    DEBUG("\tJMPTable Start: {0:x}".format(jstart))
    seg_start = idc.SegStart(jstart)

    if seg_start != idc.BADADDR:
        I.jump_table.offset_from_data = jstart - seg_start
        DEBUG("\tJMPTable offset from data: {:x}".format(I.jump_table.offset_from_data))

    I.jump_table.zero_offset = 0
    i = 0
    entries = si.get_jtable_size()
    entries = sanityCheckJumpTableSize(inst, entries)
    for i in xrange(entries):
        je = readers[jsize](jstart+i*jsize)
        # check if this is an offset based jump table
        if si.flags & idaapi.SWI_ELBASE == idaapi.SWI_ELBASE:
            # adjust jump target based on offset in table
            # we only ever see these as 32-bit offsets, even
            # when looking at 64-bit applications
            je = 0xFFFFFFFF & (je + si.elbase)

        I.jump_table.table_entries.append(je)

        DEBUG("\t\tAdding JMPTable {0}: {1:x}".format(i, je))
    #je = idc.GetFixupTgtOff(jstart+i*jsize)
    #while je != -1:
    #    I.jump_table.table_entries.append(je)
    #    if je not in RECOVERED_EAS: 
    #        new_eas.add(je)
    #    DEBUG("\t\tAdding JMPTable {0}: {1:x}".format( i, je))
    #    i += 1
    #    je = idc.GetFixupTgtOff(jstart+i*jsize)

_ELF_THUNKS = {}
_NOT_ELF_THUNKS = set()

def isElfThunkByStructureImpl(ea):
    """Try to manually identify an ELF thunk by its structure."""
    insn_t, _ = decode_instruction(ea)
    if not insn_t or not is_unconditional_jump(insn_t):
        return False, None

    real_ext_ref = None
    for cref in idautils.CodeRefsFrom(ea, 0):
        if isExternalReference(cref):
            real_ext_ref = cref
            break

    if real_ext_ref is None:
        for dref in idautils.DataRefsFrom(ea):
            if idc.SegName(dref).lower() in ".got.plt":
                # this is an external call after all
                for extref in idautils.DataRefsFrom(dref):
                    if isExternalReference(extref):
                        real_ext_ref = extref

    if real_ext_ref is not None:
        return True, handleExternalRef(getFunctionName(real_ext_ref))

    return False, None

def isThunkByFlags(ea):
    """Try to identify an ELF thunk based off of the IDA flags.

    IDA seems to have a kind of thunk-propagation. So if one thunk calls
    another thunk, then the former thing is treated as a thunk. The former
    thing will not actually follow the 'structured' form matched above, so
    we'll try to recursively match to the 'final' referenced thunk."""
    global _ELF_THUNKS

    flags = idc.GetFunctionFlags(ea)
    if 0 >= flags or not (flags & idaapi.FUNC_THUNK):
        return False, None
    
    ea_name = getFunctionName(ea)
    insn_t, _ = decode_instruction(ea)
    if not insn_t:
        DEBUG("{} at {:x} is a thunk with no code??".format(ea_name, ea))
        return False, None

    # Recursively find thunk-to-thunks.
    if is_direct_jump(insn_t) or is_direct_function_call(insn_t):
        targ_ea = get_direct_branch_target(insn_t)
        targ_is_thunk, targ_thunk_name = isElfThunk(targ_ea)
        if targ_is_thunk:
            DEBUG("Found thunk-to-thunk {:x} -> {:x}: {} to {}".format(
                ea, targ_ea, ea_name, targ_thunk_name))
            return True, targ_thunk_name
        DEBUG("XXX targ_ea={:x} is not thunk".format(targ_ea))

    if not isExternalReference(ea):
        return False, None

    # # This has the structure of a PLT thunk, but is more like an 'internal'
    # # thunk. This comes up, for example, with `sqlite3MallocSize`, where this
    # # function goes and tail-calls via a global function pointer, where the
    # # pointer is initialized on init based on config options.
    # elif is_indirect_jump(insn_t):
    #     return False, None
    #     # refs = get_instruction_references(insn_t, PIE_MODE)
    #     # if len(refs):
    #     #     data_addr = refs[0].addr
    #     #     if ".got" not in idc.SegName(data_addr).lower():
    #     #         return False, ea_name

    # DEBUG("{} at {:x} is a thunk by flags".format(ea_name, ea))

    return True, handleExternalRef(ea_name)

def isElfThunk(ea):
    """Try to figure out if a function is actually an ELF thunk, i.e. a function
    that represents a 'local' definition for an external function. Thunks work
    by having the local function jump through a function pointer that is
    resolved at runtime."""
    global _ELF_THUNKS, _NOT_ELF_THUNKS

    if ea in _ELF_THUNKS:
        return _ELF_THUNKS[ea]

    if ea in _NOT_ELF_THUNKS:
        _NOT_ELF_THUNKS.add(ea)
        return False, None

    # Try two approaches to detecting whether or not
    # something is a thunk.
    is_thunk, name = isElfThunkByStructureImpl(ea)
    if not is_thunk:
        is_thunk, name = isThunkByFlags(ea)
    
    if not is_thunk:
        _NOT_ELF_THUNKS.add(ea)
        return False, None
    else:
        _ELF_THUNKS[ea] = (is_thunk, name)
        return is_thunk, name

def opAtOffset(insn_t, off):

    if insn_t is None:
        return None

    for op in insn_t.Operands:
        
        if op.offb == off:
            if op.type in [idaapi.o_displ, idaapi.o_phrase]:
                return "MEM"

            if op.type in [idaapi.o_imm, idaapi.o_mem, idaapi.o_near, idaapi.o_far]:
                return "IMM"

            DEBUG("ERROR: Unknown op type {}, assuming MEM".format(op.type))
            return "MEM"

    return None

def instructionHandler(M, B, insn_t, inst_bytes, addr, new_func_eas):

    #DEBUG("\t\tinst: {0}".format(idc.GetDisasm(addr)))
    #DEBUG("\t\tBytes: {0}".format(inst_bytes))

    I = addInst(B, addr, insn_t, inst_bytes, new_func_eas)
    if isTrap(insn_t) or isHlt(insn_t):
        I.local_noreturn = True
        return I, True

    if isJmpTable(insn_t):
        DEBUG("Its a jump table")
        handleJmpTable(I, addr)
        return I, True

    # mark that this is an offset table
    if PIE_MODE and addr in OFFSET_TABLES:
        table_va = OFFSET_TABLES[addr].start_addr
        DEBUG("JMP at {:08x} has offset table {:08x}".format(addr, table_va))
        I.offset_table_addr = table_va

    crefs_from_here = idautils.CodeRefsFrom(addr, 0)

    #check for code refs from here
    crefs = []

    # pull code refs from generator into a list
    for cref_i in crefs_from_here:
        crefs.append(cref_i)

    isize = len(inst_bytes)
    next_ea = insn_t.ea + insn_t.size
    had_refs = False

    #true: jump to where we have a code-ref
    #false: continue as we were
    if is_conditional_jump(insn_t):
        I.true_target = crefs[0]
        I.false_target = next_ea

    return I, is_control_flow(insn_t) and not is_function_call(insn_t)


WEAK_SYMS = set()
OS_NAME = ""

def parseDefsFile(df):
    global OS_NAME, WEAK_SYMS
    emap = {}
    emap_data = {}
    is_linux = OS_NAME == "linux"
    for l in df.readlines():
        #skip comments / empty lines
        l = l.strip()
        if not l or l[0] == "#":
            continue

        if l.startswith('DATA:'):
            # process as data
            (marker, symname, dsize) = l.split()
            if 'PTR' in dsize:
                dsize = getPointerSize()
            emap_data[symname] = int(dsize)

        else:
            fname = args = conv = ret = sign = None
            line_args = l.split()
            if len(line_args) == 2:
                fname, conv = line_args
                if conv == "MCSEMA":
                    DEBUG("Found mcsema internal function: {}".format(fname))
                    realconv = CFG_pb2.ExternalFunction.McsemaCall
                    emap[fname] = (1, realconv, 'N', None)
                    continue
                else:
                    raise Exception("Unknown calling convention:"+str(conv))

            if len(line_args) == 4:
                (fname, args, conv, ret) = line_args
            elif len(line_args) == 5:
                (fname, args, conv, ret, sign) = line_args

            if conv == "C":
                realconv = CFG_pb2.ExternalFunction.CallerCleanup
            elif conv == "E":
                realconv = CFG_pb2.ExternalFunction.CalleeCleanup
            elif conv == "F":
                realconv = CFG_pb2.ExternalFunction.FastCall
            else:
                raise Exception("Unknown calling convention:"+str(l))

            if ret not in ['Y', 'N']:
                raise Exception("Unknown return type:"+ret)

            emap[fname] = (int(args), realconv, ret, sign)

            if is_linux:
                imp_name = "__imp_{}".format(fname)
                emap[imp_name] = emap[fname]
                WEAK_SYMS.add(imp_name)

    df.close()

    return emap, emap_data

def processExternalFunction(M, fn):
    global WEAK_SYMS

    args, conv, ret, sign = getFromEMAP(fn)
    ea = idc.LocByName(fn)
    is_weak = idaapi.is_weak_name(ea) or fn in WEAK_SYMS

    DEBUG("Program will reference external{}: {}".format(" (weak)" if is_weak else "", fn))
    extfn = M.external_funcs.add()
    extfn.symbol_name = fn
    extfn.calling_convention = conv
    extfn.argument_count = args
    extfn.is_weak = is_weak
    if ret == 'N':
        extfn.has_return = True
        extfn.no_return = False
    else:
        extfn.has_return = False
        extfn.no_return = True

def processExternalData(M, dt):

    data_size = EMAP_DATA[dt]
    ea = idc.LocByName(dt)
    is_weak = idaapi.is_weak_name(ea)
    
    DEBUG("Program will reference external{}: {}".format(" (weak)" if is_weak else "", dt))

    extdt = M.external_data.add()
    extdt.symbol_name = dt
    extdt.data_size = data_size
    extdt.is_weak = is_weak

def processExternals(M):
    for fn in EXTERNALS:
        fixedn = fixExternalName(fn)
        if nameInMap(EMAP, fixedn):
            processExternalFunction(M, fixedn)
        elif nameInMap(EMAP_DATA, fixedn):
            processExternalData(M, fixedn)
        else:
            DEBUG("UNKNOWN API: {0}".format(fixedn))

def readBytesSlowly(start, end):
    bytestr = ""
    for i in xrange(start, end):
        if idc.hasValue(idc.GetFlags(i)):
            bt = idc.Byte(i)
            bytestr += chr(bt)
        else:
            #virtual size may be bigger than size on disk
            #pad with nulls
            #DEBUG("Failed on {0:x}".format(i))
            bytestr += "\x00"
    return bytestr

def handleDataRelocation(M, dref, new_eas):
    dref_size = idc.ItemSize(dref)
    if not isInData(dref, dref+dref_size):
        addDataSegment(dref, dref+dref_size)
        return dref + populateDataSegment(M, dref, dref+dref_size, new_eas)
    else:
        return dref

def getAvailableBitness():
    if (idaapi.ph.flag & idaapi.PR_USE64) != 0:
        # support 64-bit addressing
        return 64
    else:
        # no support for 64-bit, assume 32-bit
        return 32

ADDRESS_SIZE = 0

def getBitness():
    global ADDRESS_SIZE
    return ADDRESS_SIZE

def getPointerSize():
    return getBitness() / 8

def relocationSize(reloc_type):
    
    reloc_type = reloc_type & idc.FIXUP_MASK
    size_map = {
        idc.FIXUP_OFF8 : 1,
        idc.FIXUP_BYTE : 1,
        idc.FIXUP_OFF16 : 2,
        idc.FIXUP_SEG16 : 2,
        idc.FIXUP_PTR32 : 4,
        idc.FIXUP_OFF32 : 4,
        idc.FIXUP_PTR48 : 8,
        idc.FIXUP_HI8 : 1,
        idc.FIXUP_HI16 : 2,
        idc.FIXUP_LOW8 : 1,
        idc.FIXUP_LOW16 : 2,
        12: 8,}

    reloc_size = size_map.get(reloc_type, -1)
    return reloc_size


def resolveRelocation(ea):
    rtype = idc.GetFixupTgtType(ea) 

    relocSize = -1
    relocVal = -1

    if getBitness() == 64:
        if rtype == -1:
            raise Exception("No relocation type at ea: {:x}".format(ea))

        DEBUG("rtype : {0:x}, {1:x}, {2:x}".format(rtype, idc.GetFixupTgtOff(ea), idc.GetFixupTgtDispl(ea)))
        relocVal = idc.GetFixupTgtDispl(ea) +  idc.GetFixupTgtOff(ea)
    else:
        if rtype == idc.FIXUP_OFF32:
            relocVal = readDword(ea)
        elif rtype == -1:
            raise Exception("No relocation type at ea: {:x}".format(ea))
        else:
            relocVal = idc.GetFixupTgtOff(ea)

    relocSize = relocationSize(rtype)
    return relocVal, relocSize

def insertRelocatedSymbol(M, D, reloc_dest, offset, seg_offset, new_eas, itemsize=-1):
    pf = idc.GetFlags(reloc_dest)

    DS = D.symbols.add()
    DS.base_address = offset+seg_offset

    itemsize = int(itemsize)
    if itemsize == -1:
        itemsize = int(idc.ItemSize(offset))

    DEBUG("Offset: {0:x}, seg_offset: {1:x} => {2:x}".format(offset, seg_offset, reloc_dest))
    DEBUG("Reloc Base Address: {0:x}".format(DS.base_address))
    DEBUG("Reloc size: {0:x}".format(itemsize))

    if isExternalReference(reloc_dest):
        fn = getFunctionName(reloc_dest)
        ext_fn = handleExternalRef(fn)
        DEBUG("External ref from data at {:x} => {} (from {})".format(reloc_dest, ext_fn, fn))
        DS.symbol_name = "ext_{}".format(ext_fn)
        DS.symbol_size = itemsize
    elif idc.isCode(pf):
        DS.symbol_name = "sub_{0:x}".format(reloc_dest)
        DS.symbol_size = itemsize
        DEBUG("Code Ref: {0:x}!".format(reloc_dest))

        if reloc_dest not in RECOVERED_EAS:
            new_eas.add(reloc_dest)

    elif idc.isData(pf):
        reloc_dest = handleDataRelocation(M, reloc_dest, new_eas)
        DS.symbol_name = "data_{:x}".format(reloc_dest)
        DS.symbol_size = itemsize
        DEBUG("Data Ref!")
    else:
        reloc_dest = handleDataRelocation(M, reloc_dest, new_eas)
        DS.symbol_name = "data_{:x}".format(reloc_dest)
        DS.symbol_size = itemsize
        DEBUG("UNKNOWN Ref, assuming data")

def isStartOfFunction(ea):
    fname = idc.GetFunctionName(ea)
    return ea == idc.LocByName(fname)

def isSaneReference(ea):
    if isInternalCode(ea) and idc.ItemHead(ea) == ea:
        return True

    # TODO(pag): Some compilers will dedup strings. This shows up in something
    # like /bin/ls, where you have `almost-all` and `all` as two options, and
    # the latter belongs to the former. A dref to `all` doesn't show up as an
    # item head, so :-/
    elif isInData(ea, ea+1): # and idc.ItemHead(ea) == ea:
        return True
    else:
        return False

def processTable(ea, size):
    """
    Loop through a possible table of pointers
    with an occasional NULL entry permitted

    Returns True, dictionary of pointer->destination, pointer size if its a pointer table
    Returns False, {}, _ if its not a valid pointer table
    """
    global ACCESSED_VIA_JMP

    def scan_table(start, end, readsize):
        table_map = {}

        read_option = {4 : readDword,
                       8 : readQword}[readsize]

        # sanity check for xrange
        if (end - start) % readsize != 0:
            return False, table_map, readsize

        for jea in xrange(start, end, readsize):
            pword = read_option(jea)
            if isSaneReference(pword): 
                ACCESSED_VIA_JMP.add(jea)
                DEBUG("Sane table entry at: {:x}".format(pword))
            elif pword == 0:
                DEBUG("Ignoring NULL entry in possible table: {:x}".format(jea))
            else:
                DEBUG("NOT a table entry at {:x}".format(jea))
                return False, table_map, readsize 

            table_map[jea] = pword

        return True, table_map, readsize

    did_find, table, readsz = scan_table(ea, ea+size, getPointerSize())
    if did_find == False and getPointerSize() == 8:
        DEBUG("Failed to find a table, trying with smaller pointer size")
        did_find, table, readsz = scan_table(ea, ea+size, 4)

    return did_find, table, readsz

def parseSingleStruct(ea, idastruct):
    DEBUG("Parsing idastruct at {:x}".format(ea))
    # get first member offset
    first_off = idc.GetFirstMember(idastruct.tid);

    # get last member offset
    last_off = idc.GetLastMember(idastruct.tid)

    # get starting offests of all members
    ptrs = {}
    members = set()

    read_size = getPointerSize()
    read_option = {4 : readDword,
                   8 : readQword}[read_size]

    for i in xrange(first_off, last_off+1):
        mn = idc.GetMemberName(idastruct.tid, i)
        # skip padding bytes
        if mn is not None:
            members.add(mn)

    for member in members:
        DEBUG("Checking idastruct member: {}".format(member))
        member_off = idc.GetMemberOffset(idastruct.tid, member)
        assert member_off != -1
        # get element size
        member_sz = idc.GetMemberSize(idastruct.tid, member_off)
        assert member_sz > 0
        
        #if its pointer size, check for ptr
        if member_sz == read_size:
            DEBUG("\tit is pointer sized")
            # check if points to sanity
            member_ea = ea+member_off
            pword = read_option(member_ea)
            if isSaneReference(pword):
                DEBUG("\tAdding reference from {:x} => {:x}".format(member_ea, pword))
                ptrs[member_ea] = pword
            else:
                DEBUG("\tNot a sane reference ({:x})".format(pword))


    return len(ptrs) != 0, ptrs, getPointerSize()

def getStructType(ea):
    """ 
    Get type information from an ea. Used to get the structure type id
    """

    flags = idaapi.getFlags(ea)
    ti = idaapi.opinfo_t()
    oi = idaapi.get_opinfo(ea, 0, flags, ti)
    if oi is not None:
        return ti
    else:
        return None

def processStruct(ea, size):
    """
    Find pointers in a structure type
    """
    # get struct size
    idastruct = getStructType(ea)
    if idastruct is None:
        DEBUG("Could not get structure size at: {:x}".format(ea))
        return False, {}, 0

    struct_size = idc.GetStrucSize(idastruct.tid)
    
    #check if this is an array of structs
    assert size % struct_size == 0

    all_ptrs = {}

    num_iters = size / struct_size
    for i in xrange(num_iters):
        # parse a single struct
        start_ea = ea + (i * struct_size)
        worked, ptrs, ptrsize = parseSingleStruct(ea, idastruct)
        if worked:
            all_ptrs.update(ptrs)

    return True, all_ptrs, getPointerSize()

def processDataChunk(ea, size):
    """
    Determine if a data chunk has some pointers in it
    """

    # Get chunk type
    # if its a string, skip it
    if IsString(ea):
        DEBUG("Found a string at {:x}".format(ea))
        return False, {}, 0
    elif IsStruct(ea):
        DEBUG("Found a struct at {:x}".format(ea))
        return processStruct(ea, size)
    else:
        DEBUG("Found an unknown blob at {:x}, treating as table".format(ea))
        return processTable(ea, size)

#referenced from
# http://stackoverflow.com/questions/32030412/twos-complement-sign-extension-python
def sign_extend(value, bits):
    sign_bit = 1 << (bits - 1)
    return (value & (sign_bit - 1)) - (value & sign_bit)

def checkIfOffsetTable(ea):
    """
    Check if this is an offset table: that is, a table
    off offsets that when added to table base result
    in the VA of a jump target
    """

    DEBUG("LOOKING FOR OFFSET TABLE AT: {:08x}".format(ea))
    # preconditions
    # can only really do this when all sections are 
    # correctly based
    if not isLinkedElf():
        DEBUG("\t... not a linked elf");
        return False, 0, []

    # only present in PIE executables
    if not PIE_MODE:
        DEBUG("\t... not in PIE mode");
        return False

    #TODO: revisit
    if getBitness() != 64:
        DEBUG("\t... not 64-bit");
        return False, 0, []

    # check that there is a code reference
    # to ea somewhere
    refs = [x for x in idautils.DataRefsTo(ea)]
    if len(refs) == 0:
        DEBUG("\t... no refs to ea");
        return False, 0, []
    
    # assumes 32-bit offsets
    # 1: EA + EA[n] = beginning of an instruction
    entrycount = 0
    entries = []
    while True:
        entry_va = ea+entrycount*4
        entry = readDword(entry_va)
        # no null entries
        if entry == 0:
            break

        if entrycount > 0:
            refs_to_entry = list(idautils.DataRefsTo(entry_va))
            if len(refs_to_entry) > 0:
                DEBUG("\tfound other references {} to table entry {} (@ {:x}).".format(refs_to_entry, entrycount, entry_va))
                break

        dest_guess = ea + sign_extend(entry, 64)
        dest_guess &= 0xFFFFFFFFL
        # has to point to code and to the
        # start of an instruction
        if isInternalCode(dest_guess) and isSaneReference(dest_guess):
            DEBUG("\tAdded destination: {:08x}".format(dest_guess))
            entries.append(dest_guess)
            entrycount += 1
        else:
            DEBUG("\tInvalid destination: {:08x}".format(dest_guess))
            # invalid entry
            break

    # minimum here is fairly arbitrary
    return (entrycount > 1, entrycount, entries)

def createOffsetTable(M, table_start, table_entries):

    # create new OffsetTable message
    OT = M.offset_tables.add()

    # set table start
    OT.start_addr = table_start

    # loop through table_entries, and populate
    # * original data (int32, readDword(table_start + i * 4))
    # * point-to va (int64, table_entries[i])

    for idx, entry in enumerate(table_entries):
        orig_data = readDword(table_start + idx * 4)
        # orig data value at table index
        OT.table_offsets.append(orig_data)
        # destination at that index
        OT.destinations.append(entry)

    jmp_refs = set()
    for ref in idautils.DataRefsTo(table_start):
        DEBUG("Checking ref to table...")
        insn_t, _ = decode_instruction(ref)

        # check if REF points to LEA REG, <value>
        if insn_t.itype == idaapi.NN_lea and insn_t.Operands[0].type == idc.o_reg:
            DEBUG("Found a LEA")
            dest_reg = idc.GetOpnd(ref, 0)

            # get next 5 insts
            cur_head = idc.NextHead(ref)
            for i in xrange(5):
                # is it a jump?
                next_insn_t, _ = decode_instruction(cur_head)
                if next_insn_t and is_unconditional_jump(next_insn_t):
                    DEBUG("Found follow unconditional jump at {:08x}".format(cur_head))
                    # is it a JMP?
                    jmp_reg = idc.GetOpnd(cur_head, 0)
                    if jmp_reg == dest_reg:
                        # yes: add EA of JMP REG ot jmp_refs 
                        DEBUG("Found JMP using offset table {:08x} at {:08x}".format(table_start, cur_head))
                        jmp_refs.add(cur_head)
                cur_head = idc.NextHead(cur_head)
        else:
            DEBUG("NOT a lea :(")

    for jmp_ref in jmp_refs:
        OFFSET_TABLES[jmp_ref] = OT

    return jmp_refs

def scanDataForRelocs(M, D, start, end, new_eas, seg_offset):
    segtype = idc.GetSegmentAttr(start, idc.SEGATTR_TYPE)
    if segtype == idc.SEG_CODE:
        return

    i = start
    while i < end:
        if PIE_MODE:
            (is_table, ecount, entries) = checkIfOffsetTable(i)
            if is_table:
                DEBUG("FOUND AN OFFSET TABLE AT: {:08x}".format(i))
                DEBUG("Table has {} destinations:".format(ecount))
                for e in entries:
                    DEBUG("\t{:08x}".format(e))
                    # these may be the only references to certain
                    # code islands. Make sure we recover them
                    #if e not in RECOVERED_EAS:
                    #    new_eas.add(e)

                refs = createOffsetTable(M, i, entries)
                for ref in refs:
                    for e in set(entries):
                        DEBUG("Adding Offset Table XREF {} => {}".format(ref, e))
                        idc.AddCodeXref(ref, e, idc.XREF_USER|idc.fl_F)

                i += (4 * ecount) - 1

        more_dref = [d for d in idautils.DataRefsFrom(i)]
        dref_size = idc.ItemSize(i) or 1
        if len(more_dref) == 0 and dref_size == 1 and not PIE_MODE:
            DEBUG("Testing address: {0:x}... ".format(i))

            # try to read a qword first, then fall back on dword
            inc_size = 1
            if getBitness() == 64:
                pword = readQword(i)
                make_word = idc.MakeQword
                inc_size = 8
                if not isSaneReference(pword):
                    pword = readDword(i)
                    make_word = idc.MakeDword
                    inc_size = 4
            else:
                make_word = idc.MakeDword
                pword = readDword(i)
                inc_size = 4

            # check for unmakred references

            #TODO(artem) possibly add check that do more reference sanity 
            # checking, such as if pword falls in the middle of a string
            if isInData(pword, pword+1):# and idc.ItemHead(pword) == pword:
                if make_word(i):
                    idc.add_dref(i, pword, idc.XREF_USER|idc.dr_O)
                    DEBUG("making New Data Reference at: {0:x} => {1:x}".format(i, pword))
                    dref_size = inc_size
                else:
                    DEBUG("WARNING: Could not make reference at {:x}".format(i))
            # check if code and points to the beginning of an instruction
            elif isInternalCode(pword) and idc.ItemHead(pword) == pword:
                if make_word(i):
                    idc.AddCodeXref(i, pword, idc.XREF_USER|idc.fl_F)
                    DEBUG("making New Code Reference at: {0:x} => {1:x}".format(i, pword))
                    dref_size = inc_size
                else:
                    DEBUG("WARNING: Could not make reference at {:x}".format(i))
            else:
                DEBUG("not code or data ref")

        i += dref_size

    def insertReference(M, D, ea, pointsto, seg_offset, new_eas, force_size=None):
        # do not make code references for mid-function code accessed via a JMP -- 
        # they will be found via the jumptable code. This prevents the insertion
        # of lots of extra code, but could be wrong for some cases
        if ea in ACCESSED_VIA_JMP and not isStartOfFunction(pointsto):
            # bail only if we are access via JMP and not the start
            # of a function
            DEBUG("\t\tNOT ADDING REF (i): {:08x} -> {:08x}".format(ea, pointsto))
            return

        # Sometiems we can have a spurious data-to-code ref, where the code should
        # not be considered a function, because it's part of some basic block, and
        # there is a normal control-flow (fall-through) to that code. 
        if not is_code(ea) and is_code(pointsto) \
        and len(tuple(idautils.CodeRefsTo(pointsto, 1))):
            DEBUG("\t\tNOT ADDING REF (ii): {:08x} -> {:08x}".format(ea, pointsto))
            return

        DEBUG("\t\tFound a probable ref from: {0:x} => {1:x}".format(ea, pointsto))
        real_size = idc.ItemSize(pointsto)
        if force_size is None:
            reloc_size = idc.ItemSize(ea)
        else:
            reloc_size = force_size
        DEBUG("\t\tReal Ref: {0:x}, reloc size: {2}, ref size: {1}".format(pointsto, real_size, reloc_size))
        insertRelocatedSymbol(M, D, pointsto, ea, seg_offset, new_eas, reloc_size)


    i = start
    while i < end:
        DEBUG("Checking address: {:x}".format(i))
        dref_size = idc.ItemSize(i) or 1
        if dref_size > getPointerSize():
            DEBUG("Possible table/struct data at {:x}; size: {:x}".format(i, dref_size))
            (found, addrs, entry_size) = processDataChunk(i, dref_size)
            if found:
                DEBUG("Its a table/struct, adding {} references".format(len(addrs)))
                for ta in sorted(addrs.keys()):
                    if addrs[ta] != 0:
                        insertReference(M, D, ta, addrs[ta], seg_offset, new_eas, force_size=entry_size)
            else:
                DEBUG("Not a stable/struct, skipping")

        elif dref_size == getPointerSize() or dref_size == 4:
            if dref_size == 4 and getBitness() == 64:
                # check if IDA missed a qword data reference
                dw = readDword(i+4)
                if dw == 0:
                    if idc.MakeQword(i):
                        DEBUG("Making qword from 32-bit dref at {:x}".format(i))
                        dref_size = 8
                    else:
                        DEBUG("WARNING: Failed at make qword at {:x}, ignoring ref".format(i))
                        dref_size = 4
                        i += dref_size
                        continue

                else:
                    DEBUG("WARNING: could not make qword from 32-bit dref at {:x}, ignoring ref".format(i))
                    dref_size = 4
                    i += dref_size
                    continue

            more_cref = [c for c in idautils.CodeRefsFrom(i,0)]

            # sanity check IDA
            more_cref = filter(lambda x: idc.ItemHead(x) == x, more_cref)

            more_dref = [d for d in idautils.DataRefsFrom(i)]
            more_dref.extend(more_cref)
            # do this check since IDA is crazy and sometimes returns data
            # references > 0xff00000000000000
            if len(more_dref) > 0 and more_dref[0] < 0xff00000000000000:
                DEBUG("\t\tFound a probable ref from: {0:x} => {1:x}".format(i, more_dref[0]))
                if len(more_dref) == 1:
                    insertReference(M, D, i, more_dref[0], seg_offset, new_eas)
                else: 
                    DEBUG("\t\tWARNING: Possible data ref problem");
                    insertReference(M, D, i, more_dref[0], seg_offset, new_eas)

        i += dref_size

def processRelocationsInData(M, D, start, end, new_eas, seg_offset):

    if start == 0:
        start = 1

    i = idc.GetNextFixupEA(start-1)

    DEBUG("Looking for relocations in {:x} - {:x}".format(start, end))

    if i == idc.BADADDR or i > end:
        if isLinkedElf():
            DEBUG("No relocations in binary, scanning for data references");
            # no fixups, do manual reloc searching
            scanDataForRelocs(M, D, start, end, new_eas, seg_offset)
        else:
            DEBUG("Not scanning data sections of object file for pointer-alikes")
    else:
        DEBUG("Found relocations in binary: ({:x})..".format(i))
        while i < end and i != idc.BADADDR:
            pointsto, itemsize = resolveRelocation(i)
            DEBUG("{0:x} Found reloc to: {1:x} (size: {2:x})".format(i, pointsto, itemsize))

            if not isExternalReference(pointsto):
                # do not add references in jump tables....
                # MAY BREAK EXTERNAL API CALLS
                if i in ACCESSED_VIA_JMP and not isStartOfFunction(pointsto):
                    # bail only if we are access via JMP and not the start
                    # of a function
                    DEBUG("\t\tNOT ADDING REF: {:08x} -> {:08x}".format(i, pointsto))
                else:
                    insertRelocatedSymbol(M, D, pointsto, i, seg_offset, new_eas, itemsize)
            else:
                DEBUG("{:x} is an external reference".format(i))
                insertRelocatedSymbol(M, D, pointsto, i, seg_offset, new_eas, itemsize)

            i = idc.GetNextFixupEA(i)

def inValidSegment(ea):
    if idc.SegStart(ea) == idc.BADADDR:
        return False
    return True

def addDataSegment(start, end):
    if end < start:
        raise Exception("Start must be before end")

    seg = idaapi.getseg(start)

    if not seg:
        raise Exception("Data must be in a valid segment")
    
    # if this is in an executalbe region,
    # move it to a data section
    seg_offset = 0
    # need_move = (seg.perm & idaapi.SEGPERM_EXEC) != 0
    # if need_move:
    #     free_data = findFreeData()
    #     seg_offset = free_data - start
    #     DEBUG("Data Segment {0:x} moved to: {1:x}".format(start, start+seg_offset))

    DATA_SEGMENTS[ (start, end,) ] = (start+seg_offset, end+seg_offset,)

    DEBUG("Adding data seg: {0}: {1}-{2}".format( 
        idc.SegName(start),
        hex(start+seg_offset),
        hex(end+seg_offset)))

    return seg_offset

def populateDataSegment(M, start, end, new_eas):

    (new_start, new_end) = DATA_SEGMENTS.get( (start, end,), (-1,-1) )

    if (new_start, new_end) == (-1,-1):
        raise Exception("Requested segment ({}, {}) not found".format(start, end))

    seg_offset = new_start - start

    D = M.internal_data.add()
    D.base_address = new_start

    SEGPERM_WRITE = 2
    
    seg = idaapi.getseg(start)
    if (seg.perm & SEGPERM_WRITE) == 0:
        D.read_only = True
    else:
        D.read_only = False

    D.data = readBytesSlowly(start, end)

    processRelocationsInData(M, D, start, end, new_eas, seg_offset)

    DEBUG("Adding data seg: {0}: {1}-{2}".format( 
        idc.SegName(start),
        hex(new_start),
        hex(new_end)))

    return seg_offset

def processDataSegments(M, new_eas):
    for n in xrange(idaapi.get_segm_qty()):
        seg = idaapi.getnseg(n)
        ea = seg.startEA
        segtype = idc.GetSegmentAttr(ea, idc.SEGATTR_TYPE)
        if segtype in (idc.SEG_DATA, idc.SEG_BSS, idc.SEG_CODE):
            start = idc.SegStart(ea)
            end = idc.SegEnd(ea)
            populateDataSegment(M, start, end, new_eas)

def recoverFunctionFromSet(M, F, blockset, new_func_eas):
    processed_blocks = set()

    while len(blockset) > 0:
        block_ea = blockset.pop()
        if block_ea in processed_blocks:
            raise Exception("Attempting to add same block twice: {0:x}".format(block_ea))

        processed_blocks.add(block_ea)
        B = basicBlockHandler(M, F, block_ea, blockset,
            processed_blocks, new_func_eas)

_RECOVERED_FUNCS = set()

def recoverFunction(M, fnea, new_func_eas):
    global _RECOVERED_FUNCS
    if fnea in _RECOVERED_FUNCS:
        return

    _RECOVERED_FUNCS.add(fnea)

    if not isStartOfFunction(fnea):
        DEBUG("{:x} is not a function! Not recovering.".format(fnea))
        return

    F = addFunction(M, fnea)

    blockset, term_insts = analyse_subroutine(fnea)
    for term_inst in term_insts:
        if isJmpTable(term_inst):
            DEBUG("Terminator inst {:x} in func {:x} is a jump table".format(
                term_inst.ea, fnea))
            updateWithJmpTableTargets(term_inst.ea, blockset, new_func_eas)
    
    recoverFunctionFromSet(M, F, blockset, new_func_eas)

def preprocessSegment(seg_ea):
    segtype = idc.GetSegmentAttr(seg_ea, idc.SEGATTR_TYPE)
    if segtype in (idc.SEG_DATA, idc.SEG_BSS, idc.SEG_CODE):
        addDataSegment(seg_ea, idc.SegEnd(seg_ea))

    for head in idautils.Heads(seg_ea, idc.SegEnd(seg_ea)):
        if not is_code(head):
            continue

        insn_t, _ = decode_instruction(head)
        if not insn_t:
            continue

        # This may mark some immediates as not being refs. Otherwise we
        # don't actually care about the results.
        refs = get_instruction_references(insn_t, PIE_MODE)

        si = idaapi.get_switch_info_ex(head)
        if not si or is_unconditional_jump(insn_t):
            continue

        DEBUG("Found a jmp based switch at: {0:x}".format(head))
        esize = si.get_jtable_element_size()
        readers = { 4: readDword,
                    8: readQword }
        base = si.jumps
        count = si.get_jtable_size()
        count = sanityCheckJumpTableSize(head, count)
        jmp_refs = set(idautils.CodeRefsFrom(head, 1))
        for i in xrange(count):
            fulladdr = base+i*esize
            DEBUG("Address accessed via JMP: {:x}".format(fulladdr))
            ACCESSED_VIA_JMP.add(fulladdr)
            je = readers[esize](fulladdr)
            if si.flags & idaapi.SWI_ELBASE == idaapi.SWI_ELBASE:
                # adjust jump target based on offset in table
                # we only ever see these as 32-bit offsets, even
                # when looking at 64-bit applications
                je = 0xFFFFFFFF & (je + si.elbase)

            if je not in jmp_refs:
                jmp_refs.add(je)
                DEBUG("\t\tJMPTable entry not in original; adding ref {:x} => {:x}".format(head, je))
                idc.AddCodeXref(head, je, idc.XREF_USER|idc.fl_F)
                mark_as_code(je)

def preprocessBinary(new_eas):
    
    # Loop through every function, to discover the heads of all blocks that
    # IDA recognizes. This will populate some global sets in `util.py` that
    # will help distinguish block heads.
    for seg_ea in idautils.Segments():    
        for funcea in idautils.Functions(idc.SegStart(seg_ea), idc.SegEnd(seg_ea)):
            new_eas.add(funcea)
            find_default_block_heads(funcea)
    
    # loop through every instruction and keep a list of jump tables referenced
    # in the data section. These are used so we can avoid generating unwanted
    # function entry points
    for seg_ea in idautils.Segments():
        preprocessSegment(seg_ea)

def recoverCfg(to_recover, outf, exports_are_apis=False):
    global EMAP
    M = CFG_pb2.Module()
    M.module_name = idc.GetInputFile()
    DEBUG("PROCESSING: {0}".format(M.module_name))

    our_entries = []
    entrypoints = idautils.Entries()

    exports = {}
    for index,ordinal,exp_ea, exp_name in entrypoints:
        exports[exp_name] = exp_ea
        
    new_eas = set()

    preprocessBinary(new_eas)
    processDataSegments(M, new_eas)
    
    for name in to_recover:
        if name in exports:
            ea = exports[name]
        else:
            ea = idc.LocByName(name)
            if ea == idc.BADADDR:
                raise Exception("Could not locate entry symbol: {0}".format(name))

        new_eas.add(ea)
        fwdname = isFwdExport(name, ea)

        if fwdname is not None:
            DEBUG("Skipping fwd export {0} : {1}".format(name, fwdname))
            continue

        if not isInternalCode(ea):
            DEBUG("Export {0} at {1} does not point to code; skipping".format(name, hex(ea)))
            continue
        
        if name not in EMAP:
            our_entries.append( (name, ea) )

    recovered_fns = 0

    # process main entry points
    for fname, fea in our_entries:
        DEBUG("Recovering: {0}".format(fname))
        entryPointHandler(M, fea, fname, exports_are_apis)
        new_eas.add(fea)

    while len(new_eas) > 0:
        cur_ea = new_eas.pop()
        if cur_ea in RECOVERED_EAS:
            continue

        RECOVERED_EAS.add(cur_ea)

        is_thunk, thunk_name = isElfThunk(cur_ea)
        if isExternalReference(cur_ea) or is_thunk:
            continue

        if not isInternalCode(cur_ea):
            raise Exception("Function EA not code: {0:x}".format(cur_ea))

        DEBUG("Recovering: {0}".format(hex(cur_ea)))
        recoverFunction(M, cur_ea, new_eas)
        recovered_fns += 1

    if recovered_fns == 0:
        DEBUG("COULD NOT RECOVER ANY FUNCTIONS")
        return

    mypath = path.dirname(__file__)
    processExternals(M)
    outf.write(M.SerializeToString())
    outf.close()

    DEBUG("Recovered {0} functions.".format(recovered_fns))
    DEBUG("Saving to: {0}".format(outf.name))


def isFwdExport(iname, ea):
    l = ea
    if l == idc.BADADDR:
        raise Exception("Cannot find addr for: " + iname)

    pf = idc.GetFlags(l)

    if not idc.isCode(pf) and idc.isData(pf):
        sz = idc.ItemSize(l)
        iname = idaapi.get_many_bytes(l, sz-1)
        return iname

    return None

def parseTypeString(typestr, ea):

    if "__stdcall" in typestr:
        conv = CFG_pb2.ExternalFunction.CalleeCleanup
    elif "__cdecl" in typestr:
        conv = CFG_pb2.ExternalFunction.CallerCleanup
    elif "__fastcall" in typestr:
        conv = CFG_pb2.ExternalFunction.FastCall
    elif "__usercall" in typestr:
        # do not handle this for now
        return (0, CFG_pb2.ExternalFunction.CalleeCleanup, "N")
    else:
        raise Exception("Could not parse function type:"+typestr)

    fn = idaapi.get_func(ea)
    if fn is None:
        raise Exception("Could not get function args for: {0:x}".format(ea))
    args = fn.argsize / 4

    ret = 'N'

    return args, conv, ret

def getExportType(name, ep):
    try:
        DEBUG("Processing export name: {} at: {:x}".format(name, ep))
        args, conv, ret, sign = getFromEMAP(name)
    except KeyError as ke:
        tp = idc.GetType(ep);
        if tp is None or "__" not in tp: 
            #raise Exception("Cannot determine type of function: {0} at: {1:x}".format(name, ep))
            DEBUG("WARNING: Cannot determine type of function: {0} at: {1:x}".format(name, ep))
            return (0, CFG_pb2.ExternalFunction.CalleeCleanup, "N")

        return parseTypeString(tp, ep)

    return args, conv, ret

def makeArgStr(name, declaration):

    argstr = "void"
    args, conv, ret, sign = getFromEMAP(name)

    # return blank string for void calls
    if not declaration and args == 0:
        return ""

    if declaration:
        joinstr = "int a"
    else:
        joinstr = "a"

    argl = [joinstr+str(a) for a in xrange(args)]

    if args > 0:
        argstr = ", ".join(argl)

    return argstr

def getAllExports() :
    entrypoints = idautils.Entries()
    to_recover = set()
    # recover every entry point
    for ep_tuple in entrypoints:
        (index, ordinal, ea, name) = ep_tuple
        to_recover.add(name)

    return to_recover 
    
if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--log_file", type=argparse.FileType('w'),
        default=sys.stderr,
        help="Log to a specific file. Default is stderr.")

    parser.add_argument(
        '--arch',
        help='Name of the architecture. Valid names are x86, amd64.',
        required=True)

    parser.add_argument(
        '--os',
        help='Name of the operating system. Valid names are linux, windows.',
        required=True)

    parser.add_argument(
        "--output", type=argparse.FileType('wb'), default=None,
        help="The output control flow graph recovered from this file",
        required=True)

    parser.add_argument(
        "--entrypoint", nargs='*',
        help="Symbol(s) to start disassembling from")

    parser.add_argument("--std-defs", action='append', type=str,
        default=[],
        help="std_defs file: definitions and calling conventions of imported functions and data")
    
    parser.add_argument("-e", "--exports-to-lift", type=argparse.FileType('r'),
        default=None,
        help="A file containing a exported functions to lift, one per line. If not specified, all exports will be lifted.")

    parser.add_argument("--exports-are-apis", action="store_true",
        default=False,
        help="Exported functions are defined in std_defs. Useful when lifting DLLs")
    
    parser.add_argument("-z", "--syms", type=argparse.FileType('r'), default=None,
        help="File containing <name> <address> pairs of symbols to pre-define.")

    parser.add_argument("--pie-mode", action="store_true", default=False,
        help="Assume all immediate values are constants (useful for ELFs built with -fPIE")

    args = parser.parse_args(args=idc.ARGV[1:])

    if args.log_file != os.devnull:
        _DEBUG = True
        _DEBUG_FILE = args.log_file
        DEBUG("Debugging is enabled.")

    addr_size = {"x86": 32, "amd64": 64}.get(args.arch, 0)
    ADDRESS_SIZE = addr_size
    if addr_size > getAvailableBitness():
        DEBUG("Arch {} address size is too big for IDA's available bitness {}! Did you mean to use idal64?".format(
            args.arch, getAvailableBitness()))
        idc.Exit(-1)

    if args.pie_mode:
        DEBUG("Using PIE mode.")
        PIE_MODE = True

    EMAP = {}
    EMAP_DATA = {}

    # Try to find the defs file or this OS
    OS_NAME = args.os
    os_defs_file = os.path.join(tools_disass_dir, "defs", "{}.txt".format(args.os))
    if os.path.isfile(os_defs_file):
        args.std_defs.insert(0, os_defs_file)

    # Load in all defs files, include custom ones
    for defsfile in args.std_defs:
        with open(defsfile, "r") as df:
            DEBUG("Loading Standard Definitions file: {0}".format(defsfile))
            em_update, emd_update = parseDefsFile(df)
            EMAP.update(em_update)
            EMAP_DATA.update(emd_update)

    eps = []
    try:
        if args.exports_to_lift: 
            eps = args.exports_to_lift.readlines()
        elif args.entrypoint is None:
            eps = getAllExports()

        eps = [ep.strip() for ep in eps]

    except IOError as e:
        DEBUG("Could not open file of exports to lift. See source for details")
        idc.Exit(-1)

    # for batch mode: ensure IDA is done processing
    DEBUG("Using Batch mode.")
    analysis_flags = idc.GetShortPrm(idc.INF_START_AF)
    analysis_flags &= ~idc.AF_IMMOFF
    # turn off "automatically make offset" heuristic
    idc.SetShortPrm(idc.INF_START_AF, analysis_flags)
    idaapi.autoWait()

    DEBUG("Starting analysis")
    try:
        # Pre-define a bunch of symbol names and their addresses. Useful when reading
        # a core dump.
        if args.syms:
            for line in args.syms:
                name, ea_str = line.strip().split(" ")
                ea = int(ea_str, base=16)
                if not isInternalCode(ea):
                    mark_as_code(ea)
                try_mark_as_function(ea)
                idc.MakeName(ea, name)

        myname = idc.GetInputFile()
        mypath = path.dirname(__file__)
        outpath = os.path.dirname(args.output.name)

        if args.entrypoint:
            eps.extend(args.entrypoint)

        assert len(eps) > 0, "Need to have at least one entry point to lift"

        outf = args.output
        DEBUG("CFG Output File file: {0}".format(outf.name))

        recoverCfg(eps, outf, args.exports_are_apis)
    except Exception as e:
        DEBUG(str(e))
        DEBUG(traceback.format_exc())
    
    idc.Exit(0)
