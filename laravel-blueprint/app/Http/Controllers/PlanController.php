<?php

namespace App\Http\Controllers;

use App\Services\CotasPythonService;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Storage;

class PlanController extends Controller
{
    public function store(Request $request, CotasPythonService $cotas): array
    {
        $data = $request->validate([
            'client' => ['required', 'string', 'max:150'],
            'part_number' => ['required', 'string', 'max:150'],
            'drawing_number' => ['required', 'string', 'max:150'],
            'revision' => ['nullable', 'string', 'max:50'],
            'pdf' => ['required', 'file', 'mimes:pdf'],
        ]);

        $path = $request->file('pdf')->store('incoming-plans');
        $absolutePath = Storage::path($path);

        return $cotas->analyze(
            $absolutePath,
            $data['client'],
            $data['part_number'],
            $data['drawing_number'],
            $data['revision'] ?? ''
        );
    }
}
