"""Read job limits through query-only duplicates from an explicitly owned broker.

No assignment, limit changes, source-handle closure or process termination.
Handle snapshot layout: System Informer phnt/include/ntpsapi.h.
Job structures and query classes: Microsoft winnt.h / QueryInformationJobObject.
"""
import ctypes as C
from ctypes import wintypes as W


class Entry(C.Structure):
    _fields_ = [('handle', W.HANDLE), ('handles', C.c_size_t), ('pointers', C.c_size_t),
                ('access', W.ULONG), ('type', W.ULONG), ('attributes', W.ULONG), ('reserved', W.ULONG)]


class Limits(C.Structure):
    _fields_ = [('process_time', C.c_int64), ('job_time', C.c_int64), ('flags', W.DWORD),
                ('min_working_set', C.c_size_t), ('max_working_set', C.c_size_t),
                ('active_process_limit', W.DWORD), ('affinity', C.c_size_t),
                ('priority', W.DWORD), ('scheduling', W.DWORD)]


def query_broker_jobs(broker_pid):
    kernel = C.WinDLL('kernel32', use_last_error=True)
    native = C.WinDLL('ntdll')
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.CreateJobObjectW.argtypes = [C.c_void_p, W.LPCWSTR]
    kernel.CreateJobObjectW.restype = W.HANDLE
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    kernel.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
    kernel.DuplicateHandle.restype = W.BOOL
    kernel.QueryInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    kernel.QueryInformationJobObject.restype = W.BOOL
    native.NtQueryInformationProcess.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
    native.NtQueryInformationProcess.restype = W.LONG

    def snapshot(process):
        size = 65536
        for _ in range(8):
            buffer = C.create_string_buffer(size)
            needed = W.ULONG()
            status = native.NtQueryInformationProcess(process, 51, buffer, size, C.byref(needed))
            if status == 0:
                count = C.c_size_t.from_buffer(buffer).value
                offset = 2 * C.sizeof(C.c_size_t)
                assert offset + count * C.sizeof(Entry) <= size
                return [Entry.from_buffer_copy(buffer, offset + i*C.sizeof(Entry)) for i in range(count)]
            if status & 0xffffffff not in (0xc0000004, 0xc0000023):
                raise RuntimeError(f'ProcessHandleInformation failed: {status & 0xffffffff:#x}')
            size = max(size*2, needed.value)
        raise RuntimeError('Handle snapshot exceeded bounded retry budget')

    def query(job, kind, value):
        returned = W.DWORD()
        if not kernel.QueryInformationJobObject(job, kind, C.byref(value), C.sizeof(value), C.byref(returned)):
            raise C.WinError(C.get_last_error())
        return value

    own = kernel.GetCurrentProcess()
    marker = kernel.CreateJobObjectW(None, None)
    if not marker:
        raise C.WinError(C.get_last_error())
    try:
        job_type = next(e.type for e in snapshot(own) if e.handle == marker)
    finally:
        assert kernel.CloseHandle(marker)
    process = kernel.OpenProcess(0x0400 | 0x0040, False, broker_pid)
    if not process:
        raise C.WinError(C.get_last_error())
    jobs = []
    try:
        for entry in snapshot(process):
            if entry.type != job_type:
                continue
            duplicate = W.HANDLE()
            if not kernel.DuplicateHandle(process, entry.handle, own, C.byref(duplicate), 0x0004, False, 0):
                raise C.WinError(C.get_last_error())
            try:
                # Bounded buffer is ample for single-renderer jobs; fail if incomplete.
                pids = query(duplicate, 3, C.create_string_buffer(65536))
                assigned, listed = (W.DWORD * 2).from_buffer(pids)
                assert assigned == listed and 8 + listed*C.sizeof(C.c_size_t) <= len(pids)
                values = list((C.c_size_t * listed).from_buffer(pids, 8))
                limits = query(duplicate, 2, Limits())
                ui = query(duplicate, 4, W.DWORD()).value
                jobs.append({'broker_handle': entry.handle, 'pids': values, 'ui_restrictions': ui,
                             'limits': {name: getattr(limits, name) for name, _ in limits._fields_}})
            finally:
                assert kernel.CloseHandle(duplicate)
    finally:
        assert kernel.CloseHandle(process)
    return {'broker_pid': broker_pid, 'job_type_index': job_type, 'jobs': jobs}


